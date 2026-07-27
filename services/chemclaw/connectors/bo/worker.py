"""The `bo` connector's own Temporal worker — what makes it a connector-owned durable capability.

Run it with `python -m connectors.bo.worker`. It polls one queue (`connector-bo`, the one its
manifest declares) and serves one workflow. Core's worker serves neither, and core imports neither:
`ConnectorJobWorkflow` reaches this workflow by type name across the queue, which is the whole point
of the seam — `bofire` and `botorch` are loaded in this process and nowhere else.

Deliberately a near-copy of `workers/background_worker.py`'s shape rather than a shared abstraction:
there is one connector worker today, the file is fifteen lines of registration, and a "connector
worker framework" with a single caller is the abstraction the Rule of Three exists to prevent. The
second connector worker is when to look at it again.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.logging import configure_logging, configure_telemetry
from chemclaw.temporal_client import connect
from connectors.bo.activities import evaluate_candidates, propose_initial, propose_next
from connectors.bo.workflows import BoCampaignWorkflow

logger = logging.getLogger(__name__)

# The queue this bundle's manifest names. A module constant rather than config: it is part of the
# connector's contract with core (`connector.yaml`), not a per-deployment knob — changing it means
# changing the manifest in the same commit.
TASK_QUEUE = "connector-bo"

BO_WORKFLOWS: list[type] = [BoCampaignWorkflow]
BO_ACTIVITIES: Sequence[Callable[..., Any]] = [propose_initial, propose_next, evaluate_candidates]


async def main() -> None:
    """Connect and poll the connector's own queue for BO campaigns."""
    configure_logging()
    configure_telemetry()
    client = await connect()
    worker = Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=BO_WORKFLOWS,
        activities=BO_ACTIVITIES,
    )
    logger.info("bo connector worker connected: queue=%s", TASK_QUEUE)
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
