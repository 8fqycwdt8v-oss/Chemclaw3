"""Run one connector's interactive worker — the process that makes its queued tool calls.

`python -m chemclaw.connectors.interactive_worker <connector>`. It polls
`connectors.queues.interactive_queue(<connector>)`, where the turn puts every call to a tool the
manifest lists under `queued:`, and serves the one workflow and the one activity that make such a
call (`connectors/queued_workflow.py`, `connectors/queued_call.py`).

**Its concurrency is the backpressure, so size it to the server, not to the worker.**
`worker_max_concurrent_activities` here is how many calls this process sends at once; the right
number is the server's slots divided by this Deployment's replicas, so the total in flight matches
what the server admits and the rest waits in the queue — globally, first come first served — rather
than being refused by a full pod. Too high costs a refusal and a retry within seconds
(`durable.publish.queued_tool_retry`); too low leaves slots idle. The chart sets it per connector.

Its own process rather than a mode of the bundle worker for the reason it has its own queue: an
hour-long job must never hold the slot a seconds-long answer is waiting for, and the two are scaled
on different signals.
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

    Refuses to start for a connector that is not enabled here or queues nothing: a worker polling a
    queue no turn writes to is a Deployment that looks like capacity and serves nothing.
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
