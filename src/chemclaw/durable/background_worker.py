"""The `background-jobs` worker.

Hosts light, long-running background jobs: ELN sync, note re-indexing, reports, memory
synthesis, the generic connector-job wrapper and template runs. Run it with
`python -m chemclaw.durable.background_worker` (after `make up`). A connector's own workflows
run on the bundle's own worker and queue, so this worker never imports a capability's
dependency closure.

Any number of replicas may poll the queue. Periodic jobs are Schedules under
`ScheduleOverlapPolicy.SKIP` (the server allows one run), every other activity is idempotent or
claim-based, and the one pass that must not overlap itself, the note reindex, takes a cluster-wide
lock (`core/job_lock.py`). State that is per pod (the knowledge checkout, the document share
mount) is read, never treated as the cluster's truth. `docs/guides/runbook.md` has the inventory.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.llm_gateway import refuse_unconfigured_llm_gateway
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.temporal_client import connect

# Importing the modules registers their workflows and activities; the sets this worker serves
# come from that registry.
from chemclaw.durable import artifact_eviction as _artifact_eviction  # noqa: F401
from chemclaw.durable import awaiting as _awaiting  # noqa: F401
from chemclaw.durable import check_in as _check_in  # noqa: F401
from chemclaw.durable import commitment_sync as _commitment_sync  # noqa: F401
from chemclaw.durable import connector_job as _connector_job  # noqa: F401
from chemclaw.durable import corpus_sync as _corpus_sync  # noqa: F401
from chemclaw.durable import deliver_message as _deliver_message  # noqa: F401
from chemclaw.durable import digest as _digest  # noqa: F401
from chemclaw.durable import document_sync as _document_sync  # noqa: F401
from chemclaw.durable import eln_sync as _eln_sync  # noqa: F401
from chemclaw.durable import eval_drift as _eval_drift  # noqa: F401
from chemclaw.durable import hypothesis_tournament as _hypothesis_tournament  # noqa: F401
from chemclaw.durable import label_sync as _label_sync  # noqa: F401
from chemclaw.durable import memory_jobs as _memory_jobs  # noqa: F401
from chemclaw.durable import note_index as _note_index  # noqa: F401
from chemclaw.durable import notify as _notify  # noqa: F401
from chemclaw.durable import observation_jobs as _observation_jobs  # noqa: F401
from chemclaw.durable import orchestrator as _orchestrator  # noqa: F401
from chemclaw.durable import orphaned_waits as _orphaned_waits  # noqa: F401
from chemclaw.durable import publish_results as _publish_results  # noqa: F401
from chemclaw.durable import report_workflow as _report_workflow  # noqa: F401
from chemclaw.durable import retention as _retention  # noqa: F401
from chemclaw.durable import template_activities as _template_activities  # noqa: F401
from chemclaw.durable import template_job as _template_job  # noqa: F401
from chemclaw.durable.registry import describe, registered_activities, registered_workflows
from chemclaw.durable.serve import (
    refuse_unauthenticated_worker,
    serve_worker,
    worker_interceptors,
)

logger = logging.getLogger(__name__)

# What this worker serves, read from the registry rather than restated here.
BACKGROUND_WORKFLOWS: list[type] = registered_workflows("background")
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities("background")


async def main() -> None:
    """Connect and poll the background-jobs queue: graph writes, ELN sync, jobs, templates.

    The gateway guard and the sign-in posture check run before `connect()`, because
    `template_activities.run_agent_step` builds an agent inside an activity, so this process takes
    turns. After `configure_logging`, so the refusal goes through this process's handlers.
    """
    configure_logging()
    configure_telemetry()
    refuse_unconfigured_llm_gateway()
    refuse_unauthenticated_worker()
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=BACKGROUND_WORKFLOWS,
        activities=BACKGROUND_ACTIVITIES,
        # How long an in-flight activity gets to finish after a stop signal; the chart's
        # `terminationGracePeriodSeconds` must sit above it.
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
        # Bounded because this queue's work is almost entirely database work against a pool far
        # smaller than temporalio's default of 100 concurrent activities.
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        # Bounds workflows kept resident between tasks; `tests/test_workers.py` holds it against the
        # chart's memory request.
        max_cached_workflows=settings.worker_max_cached_workflows,
        # Every activity is bound to the turn that asked for it and recorded in and out
        # (`durable/interceptor.py`); with span export on, the SDK's OpenTelemetry interceptor makes
        # a durable job a child of the launching turn.
        interceptors=worker_interceptors(),
    )
    logger.info(
        "background worker connected: address=%s namespace=%s queue=%s %s",
        settings.temporal_address,
        settings.temporal_namespace,
        settings.background_task_queue,
        describe("background"),
    )
    await serve_worker(worker, component="background-worker")


if __name__ == "__main__":
    asyncio.run(main())
