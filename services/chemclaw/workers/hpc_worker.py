"""The `hpc-jobs` worker: hosts the heavy-calculation workflows (plan step 1.1, xTB X3/X4).

Run it with `python -m workers.hpc_worker` (after `make up`). It connects to
Temporal, registers `QMJobWorkflow` (HPC/DFT) and `XtbJobWorkflow` (the xTB tasks
too slow to run inline) with their activities on the configured HPC task queue, and
polls until interrupted. Kill and restart it mid-job to see a
running workflow resume from event history — the CHECKMATE 1 durability spike.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.logging import configure_logging, configure_telemetry
from chemclaw.temporal_client import connect

# Importing the modules is what registers their workflows and activities (the same
# side-effect pattern `agents.chemclaw_agent` uses for tools). With the registry
# populated, the sets this worker serves come from it — so adding a durable capability
# to one of these modules is a decorator at its definition site, not an edit here.
from workflows import activities as _qm_activities  # noqa: F401
from workflows import qm_job as _qm_job  # noqa: F401
from workflows.registry import describe, registered_activities, registered_workflows

logger = logging.getLogger(__name__)

# What this worker serves, read from the registry rather than restated here.
HPC_WORKFLOWS: list[type] = registered_workflows("hpc")
HPC_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities("hpc")


async def main() -> None:
    """Connect, register the heavy-calculation workflows + activities, poll the HPC queue."""
    configure_logging()
    configure_telemetry()
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.hpc_task_queue,
        workflows=HPC_WORKFLOWS,
        activities=HPC_ACTIVITIES,
    )
    logger.info(
        "hpc worker connected: address=%s namespace=%s queue=%s %s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.hpc_task_queue,
        describe("hpc"),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
