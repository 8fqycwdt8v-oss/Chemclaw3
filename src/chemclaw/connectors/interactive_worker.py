"""Run one connector's interactive worker: the process that makes its queued tool calls.

`python -m chemclaw.connectors.interactive_worker <connector>`. Polls
`connectors.queues.interactive_queue(<connector>)`, where a turn puts every call to a tool the
manifest lists under `queued:`, and serves `connectors/queued_workflow.py` and
`connectors/queued_call.py`.

Its concurrency is the backpressure: set `worker_max_concurrent_activities` to the server's slots
divided by this Deployment's replicas, so the rest waits in the queue in arrival order rather than
being refused by a full pod. A separate process from the bundle worker so an hour-long job never
holds a slot a seconds-long answer is waiting for.
"""

import asyncio
import logging
import sys
from datetime import timedelta

from temporalio.worker import Worker

from chemclaw.connectors.queued_call import call_queued_tool
from chemclaw.connectors.queued_workflow import QueuedToolWorkflow
from chemclaw.connectors.queues import interactive_queue
from chemclaw.connectors.registry import ConnectorError, connector_spec
from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.temporal_client import connect
from chemclaw.durable.serve import (
    refuse_unauthenticated_worker,
    serve_worker,
    worker_interceptors,
)

logger = logging.getLogger(__name__)


async def run_interactive_worker(connector: str) -> None:
    """Poll `connector`'s interactive queue and make its queued calls.

    Refuses to start for a connector that is not enabled here or queues nothing, rather than run as
    capacity that serves nothing.
    """
    configure_logging()
    configure_telemetry()
    refuse_unauthenticated_worker()
    spec = connector_spec(connector)
    if spec.queued is None:
        raise ConnectorError(f"connector {connector!r} declares no queued tools")
    queue = interactive_queue(connector)
    client = await connect()
    worker = Worker(
        client,
        task_queue=queue,
        workflows=[QueuedToolWorkflow],
        activities=[call_queued_tool],
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        max_cached_workflows=settings.worker_max_cached_workflows,
        interceptors=worker_interceptors(),
    )
    logger.info(
        "%s interactive worker connected: queue=%s tools=%s concurrency=%d",
        connector,
        queue,
        ",".join(spec.queued.tools),
        settings.worker_max_concurrent_activities,
    )
    await serve_worker(worker, component=f"interactive-worker-{connector}")


def main(argv: list[str] | None = None) -> None:
    """Entry point: the one argument is the connector's name."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: python -m chemclaw.connectors.interactive_worker <connector>")
    asyncio.run(run_interactive_worker(args[0]))


if __name__ == "__main__":
    main()
