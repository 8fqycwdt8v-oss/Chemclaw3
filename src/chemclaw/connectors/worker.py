"""Run one bundle's own Temporal worker: the durable half of a connector-owned capability.

A bundle's modules register their workflows and activities through `chemclaw.durable.registry`
and the queue is derived from the bundle name, so every bundle worker is this one function and no
hand-maintained list can disagree with what the modules define (D-118).
"""

import asyncio
import logging
from datetime import timedelta

from temporalio.worker import Worker

from chemclaw.connectors.queues import bundle_queue
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.temporal_client import connect
from chemclaw.durable.registry import describe, registered_activities, registered_workflows
from chemclaw.durable.serve import (
    refuse_unauthenticated_worker,
    serve_worker,
    worker_interceptors,
)

logger = logging.getLogger(__name__)


async def run_bundle_worker(connector: str) -> None:
    """Poll `connector`'s own queue, serving exactly what importing its modules registered.

    The caller imports the bundle's `workflows` and `activities` for their registration side effect.
    That import is the isolation boundary: core's workers never make it, so the bundle's heavy
    dependencies load only here.
    """
    configure_logging()
    configure_telemetry()
    # Before `connect()`, so a worker with sign-in off and no stated posture never polls — the same
    # refusal core's `background-worker` makes (`durable/serve.refuse_unauthenticated_worker`).
    refuse_unauthenticated_worker()
    queue = bundle_queue(connector)
    client = await connect()
    worker = Worker(
        client,
        task_queue=queue,
        workflows=registered_workflows(queue),
        activities=registered_activities(queue),
        # A bundle's activity is the expensive science, so draining rather than killing matters most
        # here.
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
        # Unset, temporalio admits 100 concurrent activities, more than the CPU or the Postgres pool
        # can serve. A bundle whose activities are long waits (e.g. `calc`) raises it in the chart,
        # beside the memory that bounds it.
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        # The workflow cache ceiling matters most here: child workflows that core starts
        # (`durable/connector_job.py`, `hypothesis_tournament.py`, `template_activities.py`) run on
        # the bundle's queue, so their state is cached in this process.
        max_cached_workflows=settings.worker_max_cached_workflows,
        # Binds every activity to the turn that asked for it and records it in and out
        # (`durable/interceptor.py`). When span export is on the SDK's OpenTelemetry interceptor
        # makes a durable job a child span of the launching turn.
        interceptors=worker_interceptors(),
    )
    logger.info("%s connector worker connected: queue=%s %s", connector, queue, describe(queue))
    await serve_worker(worker, component=f"connector-worker-{connector}")


def main(connector: str) -> None:
    """Entry point for a bundle's `python -m connectors.<name>.worker`."""
    asyncio.run(run_bundle_worker(connector))
