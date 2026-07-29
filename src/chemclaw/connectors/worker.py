"""Run one bundle's own Temporal worker — the durable half of a connector-owned capability.

Written once now that there is a second caller, which is the condition `connectors/bo/worker.py`
named as the trigger for looking again: *"there is one connector worker today … the second
connector worker is when to look at it again."* `calc` is that second one, and `bo/worker.py` and
`calc/worker.py` had already become near-copies.

What made a shared version pointless before was that the *body* of each worker was its two
hand-maintained lists — `_WORKFLOWS` and `_ACTIVITIES` — so there was nothing to share but the
`Worker(...)` call. With the bundle's modules registering themselves through
`chemclaw.durable.registry` and the queue derived from the bundle name, there is no body left to
differ,
and no list that can silently disagree with what the module actually defines (D-118).
"""

import asyncio
import logging

from temporalio.worker import Worker

from chemclaw.connectors.queues import bundle_queue
from chemclaw.core import db
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.temporal_client import connect
from chemclaw.durable.registry import describe, registered_activities, registered_workflows

logger = logging.getLogger(__name__)


async def run_bundle_worker(connector: str) -> None:
    """Poll `connector`'s own queue, serving exactly what importing its modules registered.

    The caller imports the bundle's `workflows` and `activities` modules for their registration
    side effect and then calls this. That import is the isolation boundary the whole seam rests
    on: core's workers never import these modules, so the bundle's heavy dependencies are loaded
    in this process and nowhere else.
    """
    configure_logging()
    configure_telemetry()
    queue = bundle_queue(connector)
    client = await connect()
    worker = Worker(
        client,
        task_queue=queue,
        workflows=registered_workflows(queue),
        activities=registered_activities(queue),
    )
    logger.info("%s connector worker connected: queue=%s %s", connector, queue, describe(queue))
    # Every activity here is a coroutine on this process's one event loop, so a per-call Postgres
    # handshake is loop time stolen from task polling and heartbeats. Pooled for the worker's
    # whole life and closed on shutdown.
    async with db.pooling():
        await worker.run()


def main(connector: str) -> None:
    """Entry point for a bundle's `python -m connectors.<name>.worker`."""
    asyncio.run(run_bundle_worker(connector))
