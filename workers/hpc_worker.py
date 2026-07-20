"""The `hpc-jobs` worker: hosts the QM workflow and its activities (plan step 1.1).

Run it with `python -m workers.hpc_worker` (after `make up`). It connects to
Temporal, registers `QMJobWorkflow` and the QM activities on the configured HPC
task queue, and polls until interrupted. Kill and restart it mid-job to see a
running workflow resume from event history — the CHECKMATE 1 durability spike.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.logging import configure_logging
from chemclaw.temporal_client import connect
from workflows.activities import (
    parse_qm_output,
    poll_hpc_status,
    prepare_input,
    submit_to_hpc,
)
from workflows.qm_job import QMJobWorkflow

logger = logging.getLogger(__name__)

# The workflow + activities this worker serves on the hpc-jobs queue. Module-level so the
# registration and the startup log share one source (DRY), mirroring the background worker.
HPC_WORKFLOWS: list[type] = [QMJobWorkflow]
HPC_ACTIVITIES: Sequence[Callable[..., Any]] = [
    prepare_input,
    submit_to_hpc,
    poll_hpc_status,
    parse_qm_output,
]


async def main() -> None:
    """Connect, register the QM workflow + activities, and poll the HPC queue."""
    configure_logging()
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.hpc_task_queue,
        workflows=HPC_WORKFLOWS,
        activities=HPC_ACTIVITIES,
    )
    logger.info(
        "hpc worker connected: address=%s namespace=%s queue=%s workflows=%s activities=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.hpc_task_queue,
        [w.__name__ for w in HPC_WORKFLOWS],
        [a.__name__ for a in HPC_ACTIVITIES],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
