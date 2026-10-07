"""The scrape and probe surface for a process that is not the front door.

Workers record into the same process-wide metrics registry as the chat service; this module
gives them an HTTP surface so those metrics can be scraped and the worker can be probed:

- `GET /healthz` — liveness. Served on the worker's own event loop, so a loop wedged by a
  blocking call inside an activity stops answering and the kubelet restarts the pod.
- `GET /readyz` — readiness, delegated to a `ready` callable (for a Temporal worker,
  `worker.is_running` and a recent broker answer; see `durable/job_metrics.broker_seen_recently`).
- `GET /metrics` — the same registry the front door renders.

Unauthenticated like the front door's probes: the NetworkPolicy keeps the port inside the
cluster, and the exposition carries counts and capacity only, never a session, user or content.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from chemclaw.core.asgi import transport_bounds
from chemclaw.core.config import settings
from chemclaw.core.metrics import CONTENT_TYPE, METRICS

logger = logging.getLogger(__name__)


class _QuietServer(uvicorn.Server):
    """A uvicorn server that leaves the process's signals to the process.

    The default SIGINT/SIGTERM handlers would tear down the probe surface on SIGTERM and leave the
    worker polling, unprobeable while Kubernetes drains it. The worker owns shutdown; this server
    stops when its context manager exits. `bound` is an event set once the port is accepting.
    """

    def __init__(self, config: uvicorn.Config) -> None:
        """Build the server and the event that says its socket is up."""
        super().__init__(config)
        self.bound = asyncio.Event()

    def install_signal_handlers(self) -> None:
        """Install none, deliberately (see the class docstring)."""

    async def startup(self, sockets: list[Any] | None = None) -> None:
        """Bind as uvicorn does, then announce it — this is where `started` is set."""
        await super().startup(sockets=sockets)
        self.bound.set()


def _build_app(component: str, ready: Callable[[], bool]) -> Starlette:
    """The three routes, over the process registry and the caller's readiness predicate."""

    async def healthz(_request: Request) -> Response:
        """Liveness: this process's event loop is still turning."""
        return JSONResponse({"status": "ok", "component": component})

    async def readyz(_request: Request) -> Response:
        """Readiness: the work this process exists to do is actually happening.

        503 rather than a 200 with a "not ready" body, because a probe reads only the status code.
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
        component: What this process is (`background-worker`, `connector-worker-calc`), echoed in
            the health payloads so a probe response identifies the pod that answered it.
        ready: Called per readiness probe on the worker's event loop, so it must be cheap and
            non-blocking. Intended shape: `worker.is_running and broker_seen_recently()`.

    Yields:
        Once the port is bound and accepting.

    `CHEMCLAW_WORKER_METRICS_PORT=0` skips the surface (two workers on one developer machine);
    a deployment must not.
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
            # Logging is already configured process-wide; uvicorn's own would replace it. Access
            # logs are off because every line would be a kubelet probe.
            log_config=None,
            access_log=False,
            # No concurrency bound: a liveness probe refused for a full limit restarts a merely busy
            # pod.
            **transport_bounds(concurrency=False),
        )
    )
    serving = asyncio.create_task(server.serve())
    try:
        # Wait until the port accepts, so an early probe is not a refused connection reading as a
        # dead pod. Waiting on both means a failed bind (`bound` never set) ends the wait through
        # `serving`; the losing future is cancelled rather than left pending.
        bound = asyncio.ensure_future(server.bound.wait())
        try:
            await asyncio.wait([bound, serving], return_when=asyncio.FIRST_COMPLETED)
        finally:
            bound.cancel()
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
