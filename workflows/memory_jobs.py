"""Durable memory-synthesis jobs (plan steps 5.3, 5.4) on the background queue.

Thin Temporal wrappers over `memory.jobs`: each activity reads the full reaction set from the
configured active ingest sources (`sources.registry`, the same set the ELN sync ingests — no new
store) and proposes campaign / playbook notes via the PR-gate. Temporal Schedules drive them
periodically, like the ELN sync. No new infrastructure — only new note types produced by reusing
existing pieces (Phase 5, G1).
"""

import logging
from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from chemclaw.errors import ChemclawError
    from eln.ord import OrdReaction
    from kg.git_submitter import default_submitter
    from memory.jobs import (
        distill_playbooks,
        synthesize_campaigns,
        synthesize_optimization_campaigns,
    )
    from sources.registry import active_ingest_sources

from workflows.publish import BAD_DATA_RETRY

logger = logging.getLogger(__name__)


async def _all_reactions() -> list[OrdReaction]:
    """Read and map every reaction from the *configured active* ingest sources (the memory corpus).

    Reads the ingest halves of `settings.data_sources` (via `sources.registry`), the same source
    set the durable ELN sync ingests — so toggling `CHEMCLAW_DATA_SOURCES` changes what memory
    reasons over, and the two subsystems can never disagree on which sources exist (DUP-1). Every
    ingest half feeds the same canonical schema, so the memory layers reason over the union without
    knowing any source's shape. Adding a future source is one registry entry + one config token,
    not a change here (the "keep integrations dumb, put the reasoning above them" line).
    """
    since = datetime.min.replace(tzinfo=UTC)
    reactions: list[OrdReaction] = []
    for adapter in active_ingest_sources():
        for raw in await adapter.fetch_new_entries(since):
            try:
                reactions.append(adapter.map_to_ord(raw))
            except ChemclawError as exc:
                # A malformed entry is the sync's problem to report, not this job's — skip it
                # and move on. Catch only ChemclawError (the bad-data contract), so an
                # unexpected error surfaces instead of being silently dropped; log the skip
                # so a corpus that quietly loses reactions is diagnosable.
                logger.info("memory job skipped an unmappable ELN entry: %s", exc)
                continue
    return reactions


@activity.defn
async def synthesize_campaigns_activity() -> list[str]:
    """Detect reaction chains across the corpus and PR-gate a campaign note for each."""
    return await synthesize_campaigns(await _all_reactions(), default_submitter())


@activity.defn
async def distill_playbooks_activity() -> list[str]:
    """Distil cross-project candidates across the corpus and PR-gate a playbook note for each."""
    return await distill_playbooks(await _all_reactions(), default_submitter())


@activity.defn
async def synthesize_optimization_campaigns_activity() -> list[str]:
    """Group same-transformation runs across the corpus and PR-gate an optimization note each."""
    return await synthesize_optimization_campaigns(await _all_reactions(), default_submitter())


@workflow.defn
class CampaignSynthesisWorkflow:
    """Run episodic campaign synthesis durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Invoke the campaign-synthesis activity."""
        return await workflow.execute_activity(
            synthesize_campaigns_activity,
            start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )


@workflow.defn
class PlaybookDistillationWorkflow:
    """Run semantic playbook distillation durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Invoke the playbook-distillation activity."""
        return await workflow.execute_activity(
            distill_playbooks_activity,
            start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )


@workflow.defn
class OptimizationCampaignWorkflow:
    """Run episodic optimization-campaign grouping durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Invoke the optimization-campaign synthesis activity."""
        return await workflow.execute_activity(
            synthesize_optimization_campaigns_activity,
            start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
            retry_policy=BAD_DATA_RETRY,
        )
