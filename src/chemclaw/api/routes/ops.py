"""The operator surfaces: liveness, readiness, the Prometheus exposition, and schedule health.

`/healthz`, `/readyz` and `/metrics` are the three deliberately unauthenticated routes — a kubelet
and a Prometheus scrape cannot present a bearer token — and `tests/test_route_auth_coverage.py`
pins that allowlist to exactly these. `/schedules` lives beside them because it answers the same
audience: an operator asking "is the machinery running", not a chemist asking about chemistry.
"""

import logging
import time
from http import HTTPStatus

import psycopg
from fastapi import FastAPI, Request
from starlette.responses import Response

from chemclaw.api import app as front_door
from chemclaw.api.deps import CurrentUser
from chemclaw.api.state import state
from chemclaw.connectors.health import ConnectorHealth
from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS
from chemclaw.durable.schedules import ScheduleHealth, describe_schedules

log = logging.getLogger(__name__)


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


async def _database_reachable(request: Request) -> bool:
    """Whether Postgres answers, re-probed at most once per `service_readiness_cache_seconds`.

    Cached on the same window and for the same reason as the connector sweep: this route is
    unauthenticated by necessity and runs every ten seconds per pod, so an uncached probe is a
    database round trip any caller can trigger at will.

    Bounded by `service_readiness_db_timeout_seconds`, its **own** short budget rather than the
    pool's. That distinction is the whole reason a probe is safe here. `connectors/server.py`
    deliberately does not hold its readiness on the database, because an unreachable one would hold
    it for the full pool timeout — but that is an argument against an *unbounded* wait, not against
    answering the question. A readiness probe exists to report "not ready" quickly; a probe that
    reports it in a second is doing its job, and one that hangs for ten is the failure.

    A failure is reported, never raised: this route must answer, and `False` is the answer.
    """
    front = state(request)
    window = settings.service_readiness_cache_seconds
    now = time.monotonic()
    if window and now - front.database_probed_at < window:
        return front.database_reachable
    try:
        async with db.connection(
            settings.session_store_dsn or settings.postgres_dsn,
            statement_timeout_seconds=settings.service_readiness_db_timeout_seconds,
        ) as conn:
            await conn.execute("SELECT 1")
        reachable = True
    except (psycopg.Error, ConnectionError, TimeoutError):
        log.warning("readiness: Postgres did not answer", exc_info=True)
        reachable = False
    front.database_reachable = reachable
    front.database_probed_at = now
    return reachable


async def readyz(request: Request, response: Response) -> dict[str, str]:
    """Readiness: the agent can be built, Postgres answers, and each connector's reachability.

    **The database gates; the connectors do not**, and the asymmetry is the point. An unreachable
    connector costs the agent one capability, so its state is *reported* — hiding it would leave a
    chemist wondering why an answer got worse — and a deployment that would rather not serve at all
    in that state sets `connectors_required`, which fails startup instead. Postgres under
    `session_store="postgres"` is not a capability: the session claim, the conversation history, the
    owner lookup and the audit sink all go through it, so a pod that cannot reach it cannot serve a
    turn at all. It reported itself ready anyway until the 2026-08-05 database review — probing the
    thing that costs a capability and not the thing that costs the service
    (D-2026-08-05-readiness-answers-for-the-store-it-cannot-serve-without).

    Not gated under `session_store="memory"`, where there is no store to answer for.

    503 rather than an exception, so the body still names what failed — a kubelet only reads the
    status, but an operator running `curl` reads the reason. `/healthz` is deliberately untouched:
    a database outage must drain these pods from the Route, not restart them into a crash loop that
    would leave nothing to serve the moment it comes back.

    Both probes are cached for `service_readiness_cache_seconds`. Caching does not weaken the
    connector signal (reported, never gating) and bounds the database one to at most one round trip
    per window per pod. Set 0 to probe every time.
    """
    state(request).agent()
    health = await _connector_health(request)
    ready = True
    if settings.session_store == "postgres":
        ready = await _database_reachable(request)
    if not ready:
        response.status_code = HTTPStatus.SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "database unreachable",
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
