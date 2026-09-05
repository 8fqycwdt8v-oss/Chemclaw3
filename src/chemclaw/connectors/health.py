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

A connector with no `health_url` and no durable work is reported `unprobed`, not `healthy`. We
control our own bundles and give them `/healthz`; a third-party MCP server may expose nothing, and
guessing a path there would manufacture false alarms. `unprobed` is the truthful state for a bundle
there is nothing to ask, and it is deliberately not counted as unhealthy.

**A bundle whose capability is durable is asked a different question**
(`D-2026-08-27-a-queue-with-no-poller-is-unreachable`). `results` declares `jobs:` and no
`endpoint:`, so `health_url` returns None for it and every sweep since the seam existed reported it
`unprobed` — whether its worker fleet was at two replicas or at zero. The reachability of a durable
capability is whether *anything is polling* the queue its jobs run on, so that is what is asked:
`describe_task_queue(bundle_queue(name))`. No poller is `unpolled`, which counts in the gauge and
trips `connectors_required` exactly as `unreachable` does, because a job started onto a queue nobody
polls is not slow — it is a chemist told "running" until the 25-hour job timeout expires.

**A probe that could not run says so instead of guessing.** A broker outage is not the same fact as
a queue with no poller, and reporting one as the other would turn every Temporal restart into a boot
failure (`D-2026-08-08-an-outage-is-not-a-missing-job`). So only a *successful* `DescribeTaskQueue`
produces a verdict; every failure is `unknown`, which is logged, is distinguishable from `healthy`
wherever a state is read, and neither counts nor gates. That is not a degraded check clearing the
gate (`D-2026-08-08-a-degraded-check-must-not-clear-the-gate`): the sin there was a broken judge
emitting the *same* verdict a working one emits, and `unknown` is a state no working probe returns.

**A bundle this deployment cannot open at all is `unusable`, and it is asked before the socket**
(`registry.unusable_reason`). A `transport: stdio` bundle with the transport turned off has a
perfectly answerable `/healthz` and no reachable tool, so it used to report `unprobed` here —
explicitly not counted and not gating — while killing every turn outright from
`registry.mcp_connections`. Now `mcp_connections` degrades past it and this counts and gates it
exactly as `unreachable` does, which is the half that keeps degrading from being the same thing as
hiding. Unlike an outage it will not clear on its own, which is what the detail says.

**What this still cannot see is a bundle whose bearer variable is unset**, and that is stated
rather than left to be discovered: the probe is unauthenticated by design, so a server whose
credential this deployment does not hold answers 200 and reads `healthy` while every tool call is
refused. It is the same class of fault and belongs in the same verdict; `unusable_reason` records
why adding it needs its own decision rather than a line here.

**And there is a fourth consumer now: the per-turn open path**
(`D-2026-08-27-the-breaker-is-the-readiness-verdict-already-taken`). Every verdict this sweep
reaches is recorded in `connectors.reachability`, which `connectors.transport` reads before
dialling — so a connector this pod has just found unreachable does not cost
`connector_open_timeout_seconds` again on the next turn. That memory lives in its own module rather
than here because this one imports `connectors.registry`, which imports `connectors.transport`: a
reader in the transport would close the cycle. **Only the HTTP half feeds it**: the breaker decides
whether to dial an MCP endpoint, and a bundle's queue verdict says nothing about that socket.
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

from chemclaw.connectors.queues import bundle_queue
from chemclaw.connectors.reachability import record_reachability
from chemclaw.connectors.registry import enabled, health_url, unusable_reason
from chemclaw.core.config import settings
from chemclaw.core.errors import SubsystemUnavailableError
from chemclaw.core.temporal_client import connect

logger = logging.getLogger(__name__)

ConnectorState = Literal["healthy", "unreachable", "unusable", "unpolled", "unknown", "unprobed"]

#: The states that mean "this connector's capability cannot be used right now". The gauge and the
#: `connectors_required` gate both read this rather than each naming their own set, because the two
#: answers must be the same answer — a metric that alerts on one state while startup refuses on
#: another is two definitions of "down" (D-2026-08-27).
UNHEALTHY_STATES: frozenset[ConnectorState] = frozenset({"unreachable", "unusable", "unpolled"})


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

    @property
    def unhealthy(self) -> bool:
        """Whether this verdict counts as a connector being down, for the gauge and the gate.

        A property rather than a comparison at each reader, so "which states are down" has one
        definition. `unknown` is deliberately not one of them: see the module docstring.
        """
        return self.state in UNHEALTHY_STATES


class ConnectorsUnavailable(RuntimeError):
    """`connectors_required` is set and at least one enabled connector could not be reached."""


async def _probe(client: httpx.AsyncClient, name: str, url: str, budget: float) -> ConnectorHealth:
    """Probe one connector's health endpoint, bounded by `budget` seconds of wall clock.

    Any 2xx counts as healthy: a health route's contract is its status, and demanding a body shape
    would couple us to every connector's internals — including third-party servers we do not own.

    The client is passed in rather than built here: one per connector meant six TCP setups (and,
    behind an mTLS ingress, six handshakes) on every readiness probe, which the kubelet runs every
    10 seconds per pod.

    **`asyncio.wait_for`, not the client's `timeout=`, is what makes the budget a budget.** httpx's
    is a *per-operation* timeout: the read leg restarts it on every socket read, so an endpoint
    trickling one byte at a time is never late and never done — measured against the shipped 2 s
    budget, a `/healthz` emitting a byte every 1.5 s held this function for **16.6 s** and then
    reported `healthy`. The connect leg has the same shape one level down, because httpcore charges
    the connect timeout separately to the TCP connect and to the TLS handshake, so the connect
    phase alone can be charged more than once — *that* half is unmeasured here and stated as the
    API's shape rather than as a number: a stalled handshake after an instant loopback connect
    costs one charge (2.01 s against 2.0 s), and the doubling needs a slow-but-succeeding connect,
    which is a property of a network rather than of a socket. The wall clock bounds both either
    way, which is why it is the fix rather than a tighter kwarg. This is the same correction the
    queue half
    took (`_probe_queues`), for the same reason: `/readyz` is inside a kubelet probe whose
    `timeoutSeconds` is *derived* from this number, and a derivation is only honest if the number
    bounds the whole answer.

    The bound is **per endpoint** here where the queue half bounds its whole leg, and the
    difference is structural rather than stylistic: the queue half shares one `connect()`, so a
    per-bundle bound could not describe the time the shared connect already spent, while HTTP
    probes share only a connection pool and are otherwise independent. Bounding each one keeps the
    per-connector verdict — a fleet where one endpoint is dark and five answer reports exactly
    that, rather than one `unreachable` verdict smeared over all six — and the sweep still comes
    back inside one budget because the probes run concurrently.
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
        # The readmission half of the breaker: this sweep runs every readiness probe, so a
        # connector that came back is dialled again on the very next turn rather than waiting out
        # `connector_breaker_window_seconds`.
        record_reachability(name, reachable=True)
        return ConnectorHealth(name=name, state="healthy")
    record_reachability(name, reachable=False)
    return ConnectorHealth(
        name=name, state="unreachable", detail=f"health check returned {response.status_code}"
    )


async def _probe_queue(client: Client, name: str, queue: str, budget: float) -> ConnectorHealth:
    """Ask Temporal whether anything is polling this bundle's queue.

    `TASK_QUEUE_TYPE_WORKFLOW` rather than the activity queue: every bundle that declares a job
    registers a workflow for it (`connector-validate` refuses a job whose workflow its own modules
    do not register), while a bundle whose activities all live elsewhere would have an idle activity
    queue and a perfectly healthy fleet — so the activity type can be zero without anything being
    wrong.

    **Only a successful response is a verdict.** A queue nobody has ever polled is not an error —
    Temporal answers with an empty poller list — so there is no status code that means `unpolled`
    and no reason to interpret one. Every failure, RPC or otherwise, is `unknown`: an outage, a
    namespace that does not exist and a broker that is merely slow are all "we could not measure",
    and each would be a different lie if reported as "no worker is polling".

    Broad `except` because this function is inside a sweep whose contract is that it never raises,
    and because every exception here means exactly that one thing. `CancelledError` is a
    `BaseException` and is deliberately not caught: a cancelled sweep is not a verdict.
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
        detail=(
            f"no worker is polling {queue!r}, so this bundle's jobs would be accepted and never "
            "run — check the connector-worker deployment's replicas"
        ),
    )


async def _probe_endpoints(targets: list[tuple[str, str]], budget: float) -> list[ConnectorHealth]:
    """Probe every HTTP health route concurrently, over one client for the whole sweep.

    The client keeps its `timeout=` as well as the per-probe wall clock, and the two are not
    redundant: the kwarg is what stops a *socket* operation, so a probe that `wait_for` cancels
    does not leave a half-open connection in the pool for the next sweep to inherit. What it is
    not — and was relied on to be — is a bound on the answer. See `_probe`.
    """
    if not targets:
        return []
    async with httpx.AsyncClient(timeout=budget, trust_env=False) as client:
        return list(
            await asyncio.gather(*(_probe(client, name, url, budget) for name, url in targets))
        )


async def _describe_queues(targets: list[tuple[str, str]], budget: float) -> list[ConnectorHealth]:
    """Connect once, then ask every queue concurrently.

    One client for the whole sweep, from the same process-wide `connect()` every durable caller
    uses — so a front door that already holds a Temporal channel does not open a second one, and a
    front door that does not gets one channel rather than one per bundle.
    """
    client = await connect()
    return list(await asyncio.gather(*(_probe_queue(client, n, q, budget) for n, q in targets)))


async def _probe_queues(targets: list[tuple[str, str]], budget: float) -> list[ConnectorHealth]:
    """Probe every durable bundle's queue, or report them all `unknown` if the broker is not there.

    **`budget` is the bound for this half, not for each step in it.**
    The connect and the RPC used to carry that bound one each, so a broker reachable enough to
    accept a connection and then blackhole the RPC cost twice it — and the whole sweep is what
    `/readyz` waits on, inside a kubelet probe whose *default* timeout is one second. A budget
    stated once and spent twice is what makes a probe's cost unstatable, which is the property this
    route needs: the deployment's `timeoutSeconds` is derived from this number
    (`deploy/helm/chemclaw/values.yaml`, `probes.service.readiness`), and a derivation is only
    honest if the number bounds the whole answer.

    A broker that refuses fails in milliseconds; one that blackholes the SYN would otherwise hold
    the readiness route — and startup — for the SDK's own connect timeout. `connect()` caches only
    successful clients, so a bounded failure here does not poison the singleton for the job tools.

    **Sharing one budget across the connect and the RPC is right for a poll and wrong for a boot**,
    which is why the budget is an argument rather than a read of the setting. The *first* check
    after process start pays a cold connect — the PEM files parsed, the mTLS handshake done — and
    whatever that costs is taken out of the RPC that would have distinguished `unpolled` from
    `unknown`. On a poll that trade is correct: the client is cached, the connect is free, and the
    caller is a kubelet with a stopwatch. At startup it is not: `check_connectors_at_startup` runs
    once, its verdict is irreversible for that boot, and under `connectors_required` a queue with
    no poller reported `unknown` because the handshake was slow is a fleet at zero replicas that
    passes the gate whose whole purpose is to refuse it.
    """
    if not targets:
        return []
    try:
        return await asyncio.wait_for(_describe_queues(targets, budget), budget)
    except (SubsystemUnavailableError, TimeoutError) as exc:
        # Every bundle gets the same verdict because they share the one dependency that failed —
        # whether it failed at the connect or ran the budget out on the RPC. Both are "we could not
        # measure", which is what `unknown` says and why neither counts nor gates.
        return [
            ConnectorHealth(
                name=name,
                state="unknown",
                # The type is named because a `TimeoutError` renders as the empty string, and "the
                # durable backend could not be reached to ask about 'connector-x': " is a detail
                # that stops exactly where the reason should start.
                detail=(
                    f"the durable backend could not be reached to ask about {queue!r}: "
                    f"{type(exc).__name__}: {exc}"
                ),
            )
            for name, queue in targets
        ]


async def probe_connectors(budget: float | None = None) -> list[ConnectorHealth]:
    """Probe every enabled connector concurrently; never raises, so a caller can always report.

    Args:
        budget: Seconds one connector's probe may take, in both halves. `None` — every caller on
            the hot path — is `connector_health_timeout_seconds`, read here rather than defaulted
            in the signature so a deployment (and a test) that overrides it is honoured. The one
            caller that passes something else is the startup sweep; see
            `check_connectors_at_startup`.

    Concurrent because probes are independent and a serial sweep would make startup wait for the sum
    of the timeouts rather than the slowest one. That is why the two halves are gathered as well as
    the probes inside each: a deployment with both kinds of bundle pays the slower of the HTTP fan
    out and the queue fan out, not their sum.

    Which half a bundle is in follows from what it *is*. An HTTP health route is the direct question
    and wins where there is one; a bundle with no health route but with `jobs:` is reachable exactly
    when something polls its queue; a bundle with neither has nothing to ask and stays `unprobed`.
    """
    bound = settings.connector_health_timeout_seconds if budget is None else budget
    endpoints: list[tuple[str, str]] = []
    queues: list[tuple[str, str]] = []
    unprobed: list[ConnectorHealth] = []
    unusable: list[ConnectorHealth] = []
    for manifest in enabled():
        # **Asked before the socket, because a bundle this deployment cannot open is not a bundle
        # whose host has anything to say about it** (`registry.unusable_reason`). The one reason
        # that verdict covers today is `transport: stdio` with the transport off — which leaves a
        # perfectly healthy server answering `/healthz` 200 while every tool call fails, so probing
        # first reported `healthy` for a capability that could not be used at all. **A declared
        # bearer whose variable is unset is the same class of fault and is deliberately NOT here**;
        # this comment claimed it was, contradicting the module docstring forty lines up, which is
        # the one a reader checks. `unusable_reason` records why it needs its own decision. It
        # counts and gates exactly as `unreachable` does, which is what keeps
        # `mcp_connections`' decision to degrade past it from making it silent.
        refusal = unusable_reason(manifest.name, manifest.endpoint)
        if refusal is not None:
            unusable.append(ConnectorHealth(name=manifest.name, state="unusable", detail=refusal))
            continue
        # Through the registry, never off the manifest: the deployment's `connector_urls` override
        # moves where a connector actually is, and reading the declared URL here probed the
        # loopback dev default in every cluster (D-131).
        probe_url = health_url(manifest)
        if probe_url:
            endpoints.append((manifest.name, probe_url))
        elif manifest.jobs:
            # No health route, but durable work of its own: ask the queue that work runs on.
            queues.append((manifest.name, bundle_queue(manifest.name)))
        else:
            # Nothing to ask: no endpoint and no durable work, stdio (spawned per turn), or an
            # HTTP endpoint that declares no health route.
            unprobed.append(ConnectorHealth(name=manifest.name, state="unprobed"))
    probed, polled = await asyncio.gather(
        _probe_endpoints(endpoints, bound), _probe_queues(queues, bound)
    )
    return sorted([*probed, *polled, *unprobed, *unusable], key=lambda health: health.name)


async def check_connectors_at_startup() -> list[ConnectorHealth]:
    """Probe the enabled connectors at startup, logging it and honoring `connectors_required`.

    **On its own budget — `connector_startup_health_timeout_seconds` — and not the poll's.** The
    two checks look alike and are answering under opposite constraints. A `/readyz` sweep runs
    every 10 seconds per pod inside a kubelet timeout, reuses a cached Temporal client, and is
    wrong for at most one period: speed is the property that matters, and `unknown` costs a poll.
    This sweep runs once, pays the cold connect nothing else will pay again (PEM parsing, the mTLS
    handshake), and produces a verdict that is final for the boot — under `connectors_required` it
    is the difference between refusing to serve and serving a fleet whose jobs nothing runs. Its
    cost is paid once at start, so there is no reason for it to share the poll's tight budget, and
    one good reason not to: `unpolled` needs the RPC to *answer*, and a cold connect inside a 2 s
    budget can leave too little for it.

    Returns:
        Every enabled connector's health, for the readiness route and the unhealthy gauge to read.

    Raises:
        ConnectorsUnavailable: When `connectors_required` is set and at least one enabled connector
            is unreachable — the fail-fast posture a deployment can opt into, where serving with
            a silently reduced tool surface is worse than not serving. A bundle whose queue has no
            poller is one of those: its jobs are the capability, and nothing runs them.
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
    # Its own line, and its own sentence, because it is a different fact: the probe did not run, so
    # nothing here is evidence either way. It never gates — the broker is one dependency shared by
    # every durable bundle, and refusing to start on it would make a Temporal restart a rollout
    # outage — but a check that quietly did not happen is what this WARNING exists to prevent.
    unknown = [item for item in health if item.state == "unknown"]
    if unknown:
        logger.warning(
            "connector reachability could not be determined at startup (not counted as down): %s",
            ", ".join(f"{item.name} ({item.detail})" for item in unknown),
        )
    summary = ", ".join(f"{item.name}={item.state}" for item in health)
    logger.info("connectors: %s", summary or "none enabled")
    return health
