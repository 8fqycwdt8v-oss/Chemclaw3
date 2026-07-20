"""The `hpc-jobs` worker: hosts the QM workflow and its activities (plan step 1.1).

Run it with `python -m workers.hpc_worker` (after `make up`). It connects to
Temporal, registers `QMJobWorkflow` and the QM activities on the configured HPC
task queue, and polls until interrupted. Kill and restart it mid-job to see a
running workflow resume from event history — the CHECKMATE 1 durability spike.
"""

import asyncio

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.activities import (
    parse_qm_output,
    poll_hpc_status,
    prepare_input,
    submit_to_hpc,
)
from workflows.qm_job import QMJobWorkflow


async def main() -> None:
    """Connect, register the QM workflow + activities, and poll the HPC queue."""
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.hpc_task_queue,
        workflows=[QMJobWorkflow],
        activities=[prepare_input, submit_to_hpc, poll_hpc_status, parse_qm_output],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
