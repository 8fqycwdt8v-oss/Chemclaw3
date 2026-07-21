"""The `background-jobs` worker (plan step 1.8).

Hosts light, long-running background jobs — starting with the durable BO campaign
(plan step 1d.4). Run it with `python -m workers.background_worker` (after
`make up`). Kept separate from the HPC worker so heavy and light work scale
independently on their own queues (D-006).
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.logging import configure_logging, configure_telemetry
from chemclaw.temporal_client import connect
from workflows.bo_activities import (
    evaluate_candidates,
    propose_initial,
    propose_next,
)
from workflows.bo_campaign import BoCampaignWorkflow
from workflows.bo_knowledge import write_campaign_node
from workflows.eln_sync import (
    ElnSyncWorkflow,
    load_sync_cursor,
    store_sync_cursor,
    sync_eln_entries,
)
from workflows.interaction_approval import (
    InteractionApprovalWorkflow,
    propose_confirmed_answer_activity,
)
from workflows.knowledge import write_knowledge_node
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
    distill_playbooks_activity,
    synthesize_campaigns_activity,
    synthesize_optimization_campaigns_activity,
)
from workflows.notify import record_session_event_activity
from workflows.report_workflow import (
    DevelopmentReportWorkflow,
    propose_report,
    retrieve_section,
)

logger = logging.getLogger(__name__)

# The workflows and activities this worker serves on the background-jobs queue. Module-level
# so the registration is one list (and directly assertable in tests), not buried in main().
BACKGROUND_WORKFLOWS: list[type] = [
    BoCampaignWorkflow,
    ElnSyncWorkflow,
    CampaignSynthesisWorkflow,
    PlaybookDistillationWorkflow,
    OptimizationCampaignWorkflow,
    DevelopmentReportWorkflow,
    InteractionApprovalWorkflow,
]
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = [
    propose_initial,
    propose_next,
    evaluate_candidates,
    write_knowledge_node,
    write_campaign_node,
    sync_eln_entries,
    load_sync_cursor,
    store_sync_cursor,
    synthesize_campaigns_activity,
    distill_playbooks_activity,
    synthesize_optimization_campaigns_activity,
    retrieve_section,
    propose_report,
    propose_confirmed_answer_activity,
    record_session_event_activity,
]


async def main() -> None:
    """Connect and poll the background-jobs queue for BO campaigns, graph writes, ELN sync."""
    configure_logging()
    configure_telemetry()
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=BACKGROUND_WORKFLOWS,
        activities=BACKGROUND_ACTIVITIES,
    )
    logger.info(
        "background worker connected: address=%s namespace=%s queue=%s workflows=%s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.background_task_queue,
        [w.__name__ for w in BACKGROUND_WORKFLOWS],
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
