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

# Importing the modules is what registers their workflows and activities (the same
# side-effect pattern `agents.chemclaw_agent` uses for tools). With the registry
# populated, the sets this worker serves come from it — so adding a durable capability
# to one of these modules is a decorator at its definition site, not an edit here.
from workflows import audit_verify as _audit_verify  # noqa: F401
from workflows import bo_activities as _bo_activities  # noqa: F401
from workflows import bo_campaign as _bo_campaign  # noqa: F401
from workflows import bo_knowledge as _bo_knowledge  # noqa: F401
from workflows import digest as _digest  # noqa: F401
from workflows import eln_sync as _eln_sync  # noqa: F401
from workflows import eval_drift as _eval_drift  # noqa: F401
from workflows import interaction_approval as _interaction_approval  # noqa: F401
from workflows import knowledge as _knowledge  # noqa: F401
from workflows import memory_jobs as _memory_jobs  # noqa: F401
from workflows import note_index as _note_index  # noqa: F401
from workflows import notify as _notify  # noqa: F401
from workflows import orchestrator as _orchestrator  # noqa: F401
from workflows import report_workflow as _report_workflow  # noqa: F401
from workflows import retention as _retention  # noqa: F401
from workflows.registry import describe, registered_activities, registered_workflows

logger = logging.getLogger(__name__)

# What this worker serves, read from the registry rather than restated here.
BACKGROUND_WORKFLOWS: list[type] = registered_workflows("background")
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities("background")


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
        "background worker connected: address=%s namespace=%s queue=%s %s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.background_task_queue,
        describe("background"),
    )
    await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
