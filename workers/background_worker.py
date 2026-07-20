"""The `background-jobs` worker (plan step 1.8).

Hosts light, long-running background jobs — starting with the durable BO campaign
(plan step 1d.4). Run it with `python -m workers.background_worker` (after
`make up`). Kept separate from the HPC worker so heavy and light work scale
independently on their own queues (D-006).
"""

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.temporal_client import connect
from workflows.bo_activities import (
    evaluate_candidates,
    propose_initial,
    propose_next,
)
from workflows.bo_campaign import BoCampaignWorkflow
from workflows.bo_knowledge import write_campaign_node
from workflows.eln_sync import ElnSyncWorkflow, sync_eln_entries
from workflows.knowledge import write_knowledge_node

# The workflows and activities this worker serves on the background-jobs queue. Module-level
# so the registration is one list (and directly assertable in tests), not buried in main().
BACKGROUND_WORKFLOWS: list[type] = [BoCampaignWorkflow, ElnSyncWorkflow]
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = [
    propose_initial,
    propose_next,
    evaluate_candidates,
    write_knowledge_node,
    write_campaign_node,
    sync_eln_entries,
]


async def main() -> None:
    """Connect and poll the background-jobs queue for BO campaigns, graph writes, ELN sync."""
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=BACKGROUND_WORKFLOWS,
        activities=BACKGROUND_ACTIVITIES,
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
