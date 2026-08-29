"""The `background-jobs` worker (plan step 1.8).

Hosts light, long-running background jobs: ELN sync, note re-indexing, reports, memory
synthesis, the generic connector-job wrapper and template runs. Run it with
`python -m chemclaw.durable.background_worker` (after `make up`). This is core's only
worker, and D-006's heavy/light split is intact one level down: one core queue, plus one
per bundle, each sized for its own work.

A *connector's* own workflows are not here: they run on the bundle's own worker and
queue (`connectors/calc/worker.py` on `connector-calc`), which is the point of the seam —
this worker never imports a capability's dependency closure.

**Why the chart pins this one to `replicas: 1`, and what that pin is actually protecting**
(`D-2026-08-27-what-a-second-background-worker-would-race-on`). Not the database writes: one
worker already runs `worker_max_concurrent_activities` activities and many workflow tasks at
once, so a single replica was never a serialization guarantee for anything a second *process*
could not also do. And the periodic jobs are each one Temporal Schedule under
`ScheduleOverlapPolicy.SKIP` (`durable/schedules.py`), which the *server* enforces, so a second
pod cannot produce a second concurrent run of one of them however many workers poll.

What a single replica does buy is exclusion over state that lives **in the pod**, and after the
PR-gate's cluster advisory lock closed the git half there is exactly one such dependency left:
`NoteReindexWorkflow`. `retrieval/vector_index.py::reindex_notes` retires index rows for every
note absent from *this pod's* knowledge checkout, which is an `emptyDir` refreshed by the pod's
own sidecar — so two pods are two views of the corpus, and a note one has fetched and the other
has not is indexed and retired in turn, each run logging that it retired a note that exists.
Every other activity on this queue is either serialized by its Schedule, idempotent by upsert,
claim-based, or prunes against a window far wider than a sidecar's lag. Raising the count means
giving that prune a cluster-wide view of the corpus first.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

from temporalio.worker import Worker

from chemclaw.core.config import settings
from chemclaw.core.logging import configure_logging, configure_telemetry
from chemclaw.core.temporal_client import connect

# Importing the modules is what registers their workflows and activities (the same
# side-effect pattern `agents.chemclaw_agent` uses for tools). With the registry
# populated, the sets this worker serves come from it — so adding a durable capability
# to one of these modules is a decorator at its definition site, not an edit here.
from chemclaw.durable import artifact_eviction as _artifact_eviction  # noqa: F401
from chemclaw.durable import awaiting as _awaiting  # noqa: F401
from chemclaw.durable import commitment_sync as _commitment_sync  # noqa: F401
from chemclaw.durable import connector_job as _connector_job  # noqa: F401
from chemclaw.durable import corpus_sync as _corpus_sync  # noqa: F401
from chemclaw.durable import digest as _digest  # noqa: F401
from chemclaw.durable import document_sync as _document_sync  # noqa: F401
from chemclaw.durable import eln_sync as _eln_sync  # noqa: F401
from chemclaw.durable import eval_drift as _eval_drift  # noqa: F401
from chemclaw.durable import label_sync as _label_sync  # noqa: F401
from chemclaw.durable import memory_jobs as _memory_jobs  # noqa: F401
from chemclaw.durable import note_index as _note_index  # noqa: F401
from chemclaw.durable import notify as _notify  # noqa: F401
from chemclaw.durable import observation_jobs as _observation_jobs  # noqa: F401
from chemclaw.durable import orchestrator as _orchestrator  # noqa: F401
from chemclaw.durable import publish_results as _publish_results  # noqa: F401
from chemclaw.durable import report_workflow as _report_workflow  # noqa: F401
from chemclaw.durable import retention as _retention  # noqa: F401
from chemclaw.durable import template_activities as _template_activities  # noqa: F401
from chemclaw.durable import template_job as _template_job  # noqa: F401
from chemclaw.durable.registry import describe, registered_activities, registered_workflows
from chemclaw.durable.serve import serve_worker, worker_interceptors

logger = logging.getLogger(__name__)

# What this worker serves, read from the registry rather than restated here.
BACKGROUND_WORKFLOWS: list[type] = registered_workflows("background")
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = registered_activities("background")


async def main() -> None:
    """Connect and poll the background-jobs queue: graph writes, ELN sync, jobs, templates."""
    configure_logging()
    configure_telemetry()
    client = await connect()
    worker = Worker(
        client,
        task_queue=settings.background_task_queue,
        workflows=BACKGROUND_WORKFLOWS,
        activities=BACKGROUND_ACTIVITIES,
        # How long an in-flight activity gets to finish after a stop signal before it is cancelled.
        # Here at the constructor rather than inside `serve_worker` because it is the one shutdown
        # knob a reader would look for beside the work being served, and because the chart's
        # `terminationGracePeriodSeconds` has to sit above it — a drain the kubelet SIGKILLs
        # through is not a drain.
        graceful_shutdown_timeout=timedelta(seconds=settings.worker_graceful_shutdown_seconds),
        # Beside it for the same reason: the other bound on what this process may have in flight.
        # Unset, temporalio admits 100 activities at once against a Postgres pool an order of
        # magnitude smaller — and this queue's work is almost entirely database work (the retention
        # sweep, the reindex, the chain verification, every job record).
        max_concurrent_activities=settings.worker_max_concurrent_activities,
        # Every activity this worker serves, bound to the turn that asked for it and recorded on
        # its way in and out (`durable/interceptor.py`). Here rather than in `serve_worker` for
        # the reason `graceful_shutdown_timeout` is: it is a property of what the worker *serves*,
        # and a reader looking for "why does this activity log anything" looks at the constructor.
        #
        # The SDK's OpenTelemetry interceptor rides beside it when span export is on, which is the
        # half that makes a durable job a child of the launching turn — `core/temporal_client.py`
        # writes the context on the client, this reads it here.
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
