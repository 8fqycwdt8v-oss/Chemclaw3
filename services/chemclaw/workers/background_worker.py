"""The `background-jobs` worker (plan step 1.8).

Hosts core's light, long-running background jobs — the ELN sync, the memory-synthesis and report
workflows, the periodic maintenance jobs — plus `ConnectorJobWorkflow`, the generic wrapper every
connector job runs inside. Run it with `python -m workers.background_worker` (after `make up`). Kept
separate from the HPC worker so heavy and light work scale independently (D-006).

A *connector's* own workflows are deliberately absent: they are served by that bundle's worker on
its own queue (`connectors/bo/worker.py` is the first), and core reaches them by workflow type name
through the wrapper. That is why adding a durable capability no longer adds a line to the lists
below.
"""

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

from temporalio.worker import Worker

from chemclaw.config import settings
from chemclaw.logging import configure_logging, configure_telemetry
from chemclaw.temporal_client import connect
from workflows.audit_verify import AuditChainVerifyWorkflow, check_audit_chain
from workflows.connector_job import ConnectorJobWorkflow
from workflows.digest import DigestWorkflow, acknowledge_digest, collect_digests
from workflows.eln_sync import (
    ElnSyncWorkflow,
    list_ingest_sources,
    load_sync_cursor,
    store_sync_cursor,
    sync_eln_entries,
)
from workflows.eval_drift import EvalDriftWorkflow, check_eval_drift
from workflows.interaction_approval import (
    InteractionApprovalWorkflow,
    propose_confirmed_answer_activity,
)
from workflows.knowledge import write_knowledge_node
from workflows.memory_jobs import (
    CampaignSynthesisWorkflow,
    OptimizationCampaignWorkflow,
    PlaybookDistillationWorkflow,
    PublishNoteWorkflow,
    build_campaign_notes_activity,
    build_optimization_notes_activity,
    build_playbook_notes_activity,
    publish_memory_note_activity,
)
from workflows.note_index import NoteReindexWorkflow, reindex_notes_activity
from workflows.notify import record_session_event_activity
from workflows.orchestrator import resolve_fan_out_limit
from workflows.report_workflow import (
    DevelopmentReportWorkflow,
    ReportSectionWorkflow,
    propose_report,
    retrieve_section,
)
from workflows.retention import RetentionWorkflow, prune_expired_rows

logger = logging.getLogger(__name__)

# The workflows and activities this worker serves on the background-jobs queue. Module-level
# so the registration is one list (and directly assertable in tests), not buried in main().
BACKGROUND_WORKFLOWS: list[type] = [
    ElnSyncWorkflow,
    CampaignSynthesisWorkflow,
    PlaybookDistillationWorkflow,
    OptimizationCampaignWorkflow,
    PublishNoteWorkflow,
    DevelopmentReportWorkflow,
    ReportSectionWorkflow,
    InteractionApprovalWorkflow,
    EvalDriftWorkflow,
    NoteReindexWorkflow,
    RetentionWorkflow,
    AuditChainVerifyWorkflow,
    DigestWorkflow,
    # The generic wrapper every connector job runs inside. It keeps the cross-cutting concerns
    # (idempotency, actor attribution, PR-gate publish, session push-back) in core while the
    # connector's own worker serves the child workflow — so a new durable capability adds a manifest
    # entry, never a line in this list.
    ConnectorJobWorkflow,
]
BACKGROUND_ACTIVITIES: Sequence[Callable[..., Any]] = [
    write_knowledge_node,
    list_ingest_sources,
    sync_eln_entries,
    load_sync_cursor,
    store_sync_cursor,
    build_campaign_notes_activity,
    build_playbook_notes_activity,
    build_optimization_notes_activity,
    publish_memory_note_activity,
    retrieve_section,
    propose_report,
    propose_confirmed_answer_activity,
    reindex_notes_activity,
    prune_expired_rows,
    check_audit_chain,
    collect_digests,
    acknowledge_digest,
    record_session_event_activity,
    check_eval_drift,
    resolve_fan_out_limit,
]


async def main() -> None:
    """Connect and poll the background-jobs queue for core's jobs and the connector-job wrapper."""
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
