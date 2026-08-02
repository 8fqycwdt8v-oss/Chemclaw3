"""The operator surfaces: liveness, readiness, the Prometheus exposition, and schedule health.

`/healthz`, `/readyz` and `/metrics` are the three deliberately unauthenticated routes — a kubelet
and a Prometheus scrape cannot present a bearer token — and `tests/test_route_auth_coverage.py`
pins that allowlist to exactly these. `/schedules` lives beside them because it answers the same
audience: an operator asking "is the machinery running", not a chemist asking about chemistry.
"""

import time

from fastapi import FastAPI, Request
from starlette.responses import Response

from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser
from chemclaw.api.state import state
from chemclaw.connectors.health import ConnectorHealth
from chemclaw.core.config import settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
from chemclaw.durable.schedules import ScheduleHealth, describe_schedules


async def healthz() -> dict[str, str]:
    """Liveness: the process is up."""
    return {"status": "ok"}


async def _connector_health(request: Request) -> list[ConnectorHealth]:
    """The connector sweep, re-probed at most once per `service_readiness_cache_seconds`.

    Monotonic, not wall-clock: a clock adjustment must not make the last sweep look
    arbitrarily fresh. A concurrent second caller inside the window reads the same snapshot;
    two callers racing past the window both probe once, which is a wasted sweep and not a
    correctness problem, so it is not worth a lock on a readiness route.
    """
    front = state(request)
    window = settings.service_readiness_cache_seconds
    now = time.monotonic()
    if window and now - front.connector_health_at < window:
        return front.connector_health
    # Through the front-door module so the suite's patch seam (`chemclaw.api.app.
    # probe_connectors`) keeps reaching the probe this route actually runs.
    health = await front_door.probe_connectors()
    front.connector_health = health
    front.connector_health_at = now
    return health


async def readyz(request: Request) -> dict[str, str]:
    """Readiness: the agent can be built, plus each enabled connector's reachability.

    The connector states are *reported*, not required: an unreachable connector costs the
    agent that capability, and hiding it would leave a chemist wondering why an answer got
    worse. It is re-probed here rather than read from a startup snapshot so the answer is
    current, and the probe also refreshes the `chemclaw_connectors_unhealthy` gauge — a
    readiness probe runs on the cadence a gauge wants anyway, so one bounded sweep serves
    both. A deployment that would rather not serve at all in this state sets
    `connectors_required`, which fails startup instead.

    The sweep is cached for `service_readiness_cache_seconds`. This route is unauthenticated
    by necessity (a kubelet cannot present a token) and runs every 10 seconds per pod, so an
    uncached probe is a fan-out any caller can trigger at will — N HTTP round trips per
    request against the connector fleet. Caching does not weaken the signal: the connector
    states are reported, never gating, so the only cost is that a reported state can be up to
    one window stale. Set 0 to probe every time.
    """
    state(request).agent()
    health = await _connector_health(request)
    return {
        "status": "ready",
        "connectors": ", ".join(f"{item.name}={item.state}" for item in health),
    }


async def metrics() -> Response:
    """Prometheus exposition for this pod (gap DEP-4).

    Unauthenticated on purpose, like `/healthz` and `/readyz`: a scrape happens before and
    independently of user identity. What makes that safe is the exposition itself — counts,
    capacity and an operator-chosen `profile` label, never a session id, a user, or any turn
    content, enforced by D-152's declared-label allowlist.

    It is *not* the NetworkPolicy, which this docstring used to name. A NetworkPolicy selects
    peers, not paths, and the front door's Route declares no `spec.path` — so this endpoint is
    reachable on the external host wherever the Route is enabled. `deploy/values.yaml`
    (`route.ipWhitelist`) carries the control for a deployment that will not accept that.
    """
    return Response(content=METRICS.render(), media_type=CONTENT_TYPE)


async def schedules(
    principal: CurrentUser,
) -> list[ScheduleHealth]:
    """Health of every periodic job: when it last ran, and whether it succeeded (gap SCH-4).

    Nothing reported this, so an ELN sync failing every run advanced no cursor and raised no
    alarm — it surfaced weeks later as "the agent doesn't know about recent experiments",
    the hardest class of problem to attribute.

    Read from Temporal's own schedule state rather than a second table: Temporal is already
    the authority on when a Schedule fired and how the run ended, and a mirrored table could
    only ever drift from it.
    """
    return await describe_schedules()


def register(app: FastAPI) -> None:
    """Attach this module's routes to `app` — called once, by `create_app` only.

    Registered with the app's own decorators rather than an `APIRouter` + `include_router`:
    since FastAPI 0.139 `include_router` is lazy — `app.routes` would hold opaque
    `_IncludedRouter` nodes, invisible to everything that walks the route table by type
    (`tests/test_route_auth_coverage.py`, the session-scope inventory in
    `tests/test_service.py`) — and a standalone router's routes carry no
    `dependency_overrides_provider`, which silently disables `app.dependency_overrides`.
    Registering on the app keeps both exactly as they were when these handlers lived in
    `create_app`.
    """
    app.get("/healthz")(healthz)
    app.get("/readyz")(readyz)
    app.get("/metrics")(metrics)
    app.get("/schedules")(schedules)
