"""Run a Temporal worker so that a pod termination finishes its work instead of losing it.

Python installs no `SIGTERM` handler, so without one a worker dies mid-activity on every drain,
rollout or scale-down: long activities rerun from scratch after their start-to-close timeout,
and pools and checkouts are abandoned. A signal handler calls `Worker.shutdown()` — stop polling,
let in-flight tasks finish, cancel the rest after `graceful_shutdown_timeout`.

Every worker's `main()` ends in one call here, which wires the Postgres pool, the probe/scrape
surface (`core/worker_http.py`) and this shutdown together, so no entrypoint can wire a subset.
"""

import asyncio
import logging
import signal
from functools import partial

from temporalio.worker import Interceptor, Worker

from chemclaw.core import db
from chemclaw.core.config import settings
from chemclaw.core.executor import install_default_executor
from chemclaw.core.logging import log_event
from chemclaw.core.worker_http import worker_http
from chemclaw.durable.interceptor import ChemclawWorkerInterceptor, activities_in_flight, draining
from chemclaw.durable.job_metrics import (
    bind_job_gauges,
    broker_seen_recently,
    poll_open_jobs,
)
from chemclaw.durable.job_record import log_record_durability
from chemclaw.publish.outbox import poll_backlog
from chemclaw.publish.registry import publishing_enabled

logger = logging.getLogger(__name__)

# SIGTERM is what the kubelet sends; SIGINT (Ctrl-C) makes a local worker drain the same way.
_STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)

# The one worker that drains the result outbox, and so the one that reports its backlog.
_BACKLOG_COMPONENT = "background-worker"


def refuse_unauthenticated_worker() -> None:
    """Fail closed when a Temporal worker would run with sign-in off and nobody said so.

    A worker binds no request surface, so the front door's bind-based check does not apply; but with
    `entra_required` False every activity resolves the shared dev principal and every authorization
    gate is open. So that posture must be stated with `worker_allow_unauthenticated` (set only by
    local lanes). A no-op when `entra_required` is on. Called before `connect()`, so a refused
    worker never polls; `tests/test_worker_posture.py` checks every entrypoint calls it.

    Raises:
        RuntimeError: naming the setting that proceeds and the one that should be set instead.
    """
    if settings.entra_required:
        return
    if not settings.worker_allow_unauthenticated:
        raise RuntimeError(
            "SECURITY: this Temporal worker would run with CHEMCLAW_ENTRA_REQUIRED=false — every "
            "activity it serves would run as the shared dev principal with all authorization "
            "gates OPEN. Set CHEMCLAW_ENTRA_REQUIRED=true for any shared deployment, or set "
            "CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED=true to state that an unauthenticated worker "
            "(local dev) is what you mean."
        )
    logger.warning(
        "SECURITY: this Temporal worker runs with entra_required=false and "
        "CHEMCLAW_WORKER_ALLOW_UNAUTHENTICATED set — every activity runs as the shared dev "
        "principal with all authorization gates OPEN. Right for local dev, wrong for anything "
        "shared."
    )


def worker_interceptors() -> list[Interceptor]:
    """The interceptor chain every `Worker` in this system adds to the client's own.

    One function, so every worker gets the same chain. The tracing interceptor is deliberately
    absent: the SDK prepends the client's interceptors, and `core/temporal_client.connect_options`
    already puts one there, so adding it here would trace everything twice. The client's tracing
    interceptor is therefore outermost and encloses ours.
    """
    return [ChemclawWorkerInterceptor()]


def worker_ready(worker: Worker) -> bool:
    """Whether this worker is both alive **and** still hearing from the broker.

    `worker.is_running` alone stays true through a broker outage; the second half is the freshness
    of `poll_open_jobs`, which already queries the broker on a timer. Covers runtime severing
    (broker restart, NetworkPolicy change, mTLS rotation); a worker that cannot reach the broker at
    startup exits instead. Module-level so the test drives this definition.
    """
    return worker.is_running and broker_seen_recently()


async def serve_worker(worker: Worker, *, component: str) -> None:
    """Poll until asked to stop, then drain — with the pool open and the probes answering.

    Args:
        worker: An already-built Temporal worker; what it serves differs per entrypoint, and
            `graceful_shutdown_timeout` belongs at its constructor.
        component: What this process is (`background-worker`, `connector-worker-calc`), for the
            health payloads and the log line.

    A worker fatal error propagates rather than being swallowed by the drain.
    """
    loop = asyncio.get_running_loop()
    # Before the first poll: activities offload blocking work to the default executor, and the stock
    # size can equal `worker_max_concurrent_activities`, starving everything else. See
    # `core/executor.py`.
    install_default_executor(
        component=component, reserved=settings.worker_max_concurrent_activities
    )
    # Before the probe surface opens, so the first scrape has a reading.
    bind_job_gauges()
    # And beside it, the one deployment fact a worker's own logs never carried: whether the runs it
    # is about to record are kept at all. See `job_record.log_record_durability`.
    log_record_durability(component)
    stop = asyncio.Event()
    for sig in _STOP_SIGNALS:
        loop.add_signal_handler(sig, stop.set)
    try:
        # Pooled for the worker's life, since a per-call handshake steals loop time from polling and
        # heartbeats; closed on shutdown because the signal handler lets this unwind. Readiness is
        # `worker_ready`, shared with its test.
        async with (
            db.pooling(),
            worker_http(component=component, ready=partial(worker_ready, worker)),
        ):
            running = asyncio.create_task(worker.run())
            waiting = asyncio.create_task(stop.wait())
            # Refreshes the open-jobs gauge from the broker (`durable/job_metrics.py`). Kept alive
            # through the drain so `/metrics` does not freeze during shutdown, and cancelled however
            # this function exits.
            polling = asyncio.create_task(poll_open_jobs(worker.client, stop))
            # The result outbox's backlog, for the worker that drains it: read from the table on
            # a timer so every replica reports the same truth, not the last pass it ran itself.
            backlog = (
                asyncio.create_task(poll_backlog(stop))
                if component == _BACKLOG_COMPONENT and publishing_enabled()
                else None
            )
            try:
                await asyncio.wait({running, waiting}, return_when=asyncio.FIRST_COMPLETED)
                waiting.cancel()
                if running.done():  # a fatal worker error, or a shutdown from somewhere else
                    await running
                    return
                # Log how many activities the drain is carrying: an activity cancelled by shutdown
                # is redelivered and paid for twice, and this count is the only trace of that.
                log_event(
                    logger,
                    "worker.draining",
                    "%s: draining with %d activity/activities in flight",
                    component,
                    activities_in_flight(),
                    component=component,
                    activities_in_flight=activities_in_flight(),
                    budget_seconds=settings.worker_graceful_shutdown_seconds,
                )
                # What `graceful_shutdown_timeout` does not cover is cancelled; each cancellation is
                # counted in the interceptor (`durable/interceptor.py::draining`).
                with draining():
                    await worker.shutdown()
                    await running
                log_event(
                    logger,
                    "worker.drained",
                    "%s: drained",
                    component,
                    component=component,
                )
            finally:
                polling.cancel()
                if backlog is not None:
                    backlog.cancel()
    finally:
        for sig in _STOP_SIGNALS:
            loop.remove_signal_handler(sig)
