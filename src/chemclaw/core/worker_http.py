"""The scrape and probe surface for a process that is not the front door.

Two findings, one cause: the only process in the system with an HTTP surface was the chat service,
so the only process anything could observe or probe was the chat service.

**Metrics went nowhere.** `deploy/` scraped the front door and nothing else, on the stated reasoning
that recording a metric elsewhere "is a no-op — there is no registry and no HTTP surface in those
processes". Half of that was never true: `core/metrics_bridge.py` imports the registry lazily and
the import *succeeds* in every process, because `api/metrics.py` is stdlib-only. So the background
worker and every connector worker have been incrementing counters into a live registry that nothing
could read. Every durable job launched, every note proposed from a workflow, every audit-sink
failure inside a background activity: recorded, and invisible.

**"Liveness is the Temporal poll itself"** was asserted in three chart templates and enforced
nowhere. A worker whose poll loop has died holds its process open, so Kubernetes reports `Running`,
no probe contradicts it, and — per the paragraph above — no metric was reaching anyone either. The
two gaps hid each other.

One HTTP surface closes both, which is why they are one module and not two:

- `GET /healthz` — liveness, and a stronger signal than the pod's mere existence: this route is
  served on the *worker's own event loop*, so a loop wedged by a blocking call inside an activity
  stops answering and the kubelet restarts the pod. That is the failure "the process is up" cannot
  see.
- `GET /readyz` — readiness, delegated to a `ready` callable rather than assumed. For a Temporal
  worker that is `worker.is_running`, so a worker that has not started polling, or has stopped, is
  reported not-ready instead of being asserted alive by a comment.
- `GET /metrics` — the same registry the front door renders, now with a reader.

Unauthenticated, exactly like the front door's three: a kubelet cannot present a token and a scrape
happens independently of user identity. The NetworkPolicy is what keeps the port inside the cluster,
and the exposition carries counts and capacity only — never a session, a user, or turn content.

Starlette rather than FastAPI because there is no request model, no dependency injection and no
OpenAPI document worth generating here; three routes returning three constants is what this is.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from chemclaw.api.metrics import CONTENT_TYPE, METRICS
from chemclaw.core.config import settings

logger = logging.getLogger(__name__)


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signals to the process.

    `Server.serve()` installs SIGINT/SIGTERM handlers by default, which is right for a server that
    *is* the process and wrong for one running beside a Temporal worker: the handler sets
    `should_exit`, so a SIGTERM would tear down the probe surface and leave the worker polling —
    the pod would go unprobeable at exactly the moment Kubernetes had decided to drain it. The
    worker owns the shutdown; this server stops when its context manager exits.
    """

    def install_signal_handlers(self) -> None:
        """Install none, deliberately (see the class docstring)."""


def _build_app(component: str, ready: Callable[[], bool]) -> Starlette:
    """The three routes, over the process registry and the caller's readiness predicate."""

    async def healthz(_request: Request) -> Response:
        """Liveness: this process's event loop is still turning."""
        return JSONResponse({"status": "ok", "component": component})

    async def readyz(_request: Request) -> Response:
        """Readiness: the work this process exists to do is actually happening.

        503 rather than a 200 carrying a `"status": "not-ready"` body, because a probe reads the
        status code and nothing else — a 200 saying "not ready" is a pod reporting ready.
        """
        healthy = ready()
        return JSONResponse(
            {"status": "ready" if healthy else "not-ready", "component": component},
            status_code=200 if healthy else 503,
        )

    async def metrics(_request: Request) -> Response:
        """Prometheus exposition for this process."""
        return PlainTextResponse(METRICS.render(), media_type=CONTENT_TYPE)

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/readyz", readyz),
            Route("/metrics", metrics),
        ]
    )


@asynccontextmanager
async def worker_http(*, component: str, ready: Callable[[], bool]) -> AsyncIterator[None]:
    """Serve `/healthz`, `/readyz` and `/metrics` for the body of this context manager.

    Args:
        component: What this process is (`background-worker`, `connector-worker-qm`), echoed in the
            health payloads so a probe response identifies the pod that answered it.
        ready: Called per readiness probe. Cheap and non-blocking — it runs on the worker's event
            loop, so a predicate that does I/O would make the probe part of the problem it reports
            on. `worker.is_running` is the intended shape.

    Yields:
        Once the port is bound and accepting.

    Set `CHEMCLAW_WORKER_METRICS_PORT=0` to skip the surface entirely. That is for running two
    workers on one developer machine, where the second would otherwise fail to bind — not for a
    deployment, where a worker without this surface is the state this module exists to end.
    """
    if not settings.worker_metrics_port:
        logger.info("%s: worker HTTP surface disabled (worker_metrics_port=0)", component)
        yield
        return

    server = _QuietServer(
        uvicorn.Config(
            _build_app(component, ready),
            host=settings.worker_metrics_host,
            port=settings.worker_metrics_port,
            # The worker's own logging configuration is already applied process-wide by
            # `configure_logging`; letting uvicorn install its own would replace it. Access logs
            # are off because every line would be a kubelet probe.
            log_config=None,
            access_log=False,
        )
    )
    serving = asyncio.create_task(server.serve())
    try:
        # `serve()` binds before it starts accepting, and a probe arriving in that window would be
        # a connection refused that reads as a dead pod. Waiting for the flag is bounded by the
        # bind itself; if it fails, the task raises and the wait ends with it.
        while not server.started and not serving.done():
            await asyncio.sleep(0.01)
        if serving.done():  # the bind failed - surface it rather than run unobservable
            await serving
        logger.info(
            "%s: serving /healthz /readyz /metrics on %s:%s",
            component,
            settings.worker_metrics_host,
            settings.worker_metrics_port,
        )
        yield
    finally:
        server.should_exit = True
        await serving
