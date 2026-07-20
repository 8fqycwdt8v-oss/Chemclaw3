"""Durable memory-synthesis jobs (plan steps 5.3, 5.4) on the background queue.

Thin Temporal wrappers over `memory.jobs`: each activity reads the full reaction set from the
ELN adapter (the reaction source — no new store) and proposes campaign / playbook notes via
the PR-gate. Temporal Schedules drive them periodically, like the ELN sync. No new
infrastructure — only new note types produced by reusing existing pieces (Phase 5, G1).
"""

from datetime import UTC, datetime, timedelta

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from eln.adapter import ElnAdapter
    from eln.json_adapter import JsonExportAdapter
    from eln.ord import OrdReaction
    from eln.ord_adapter import OrdJsonAdapter
    from kg.git_submitter import default_submitter
    from memory.jobs import (
        distill_playbooks,
        synthesize_campaigns,
        synthesize_optimization_campaigns,
    )

from workflows.publish import BAD_DATA_RETRY


async def _all_reactions() -> list[OrdReaction]:
    """Read and map every reaction from every ELN source (the corpus the memory jobs reason over).

    Both ingestion adapters — free-text and native ORD — feed the same canonical schema, so
    the memory layers reason over the union without knowing either source's shape. Adding a
    future source is one more adapter here, not a change to any memory job (the "keep
    integrations dumb, put the reasoning above them" line).
    """
    since = datetime.min.replace(tzinfo=UTC)
    adapters: list[ElnAdapter] = [JsonExportAdapter(), OrdJsonAdapter()]
    reactions: list[OrdReaction] = []
    for adapter in adapters:
        for raw in await adapter.fetch_new_entries(since):
            try:
                reactions.append(adapter.map_to_ord(raw))
            except ValueError:
                continue  # a malformed entry is the sync's problem to report, not this job's
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
