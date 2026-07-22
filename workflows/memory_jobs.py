"""Durable memory-synthesis jobs (plan steps 5.3, 5.4) on the background queue.

Thin Temporal wrappers over `memory.jobs`: each activity reads the full reaction set from the
configured active ingest sources (`sources.registry`, the same set the ELN sync ingests — no new
store) and proposes campaign / playbook notes via the PR-gate. Temporal Schedules drive them
periodically, like the ELN sync. No new infrastructure — only new note types produced by reusing
existing pieces (Phase 5, G1).
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.config import settings
    from chemclaw.errors import ChemclawError
    from eln.ord import OrdReaction
    from kg.git_submitter import default_submitter
    from kg.note import Note
    from kg.pr_gate import propose_note
    from memory.jobs import (
        build_campaign_notes,
        build_optimization_notes,
        build_playbook_notes,
    )
    from sources.registry import active_ingest_sources

from workflows.orchestrator import fan_out
from workflows.publish import BAD_DATA_RETRY, note_publish_retry

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
async def build_campaign_notes_activity() -> list[Note]:
    """Detect reaction chains across the corpus and build (not publish) one campaign note each."""
    return build_campaign_notes(await _all_reactions())


@activity.defn
async def build_playbook_notes_activity() -> list[Note]:
    """Distil cross-project candidates across the corpus and build a playbook note per candidate."""
    return build_playbook_notes(await _all_reactions())


@activity.defn
async def build_optimization_notes_activity() -> list[Note]:
    """Group same-transformation runs across the corpus and build an optimization note per group."""
    return build_optimization_notes(await _all_reactions())


@activity.defn
async def publish_memory_note_activity(note: Note) -> str:
    """PR-gate one already-built memory note; return its reference (the fan-out publish step)."""
    return await propose_note(note, default_submitter())


@workflow.defn
class PublishNoteWorkflow:
    """Publish one memory note through the PR-gate — the fan-out unit of a synthesis job (F10-D2).

    Each proposed note is its own child workflow so a single poison note (a bad git write that
    exhausts its retries) is isolated and dropped by the fan-out (D-030), while the rest of the
    corpus's notes still land — instead of one note failing the whole synthesis batch.
    """

    @workflow.run
    async def run(self, note: Note) -> str:
        """Run the PR-gate publish activity for one note with the bounded note-write retry."""
        return await workflow.execute_activity(
            publish_memory_note_activity,
            note,
            start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
            retry_policy=note_publish_retry(),
        )


async def _synthesize(build_activity: Any, id_prefix: str) -> list[str]:
    """Build the notes in one activity, then fan each out to a `PublishNoteWorkflow` child (DRY).

    The three synthesis jobs differ only in which builder runs; the detect-then-fan-out topology is
    identical, so it lives here once. Detection reads the whole corpus (one activity); publishing is
    per-note and independent (one child each), so a slow or failing note never blocks the others.
    """
    notes = await workflow.execute_activity(
        build_activity,
        start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
        retry_policy=BAD_DATA_RETRY,
    )
    return await fan_out(PublishNoteWorkflow, notes, id_prefix=id_prefix)


@workflow.defn
class CampaignSynthesisWorkflow:
    """Run episodic campaign synthesis durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Detect chains, then fan each campaign note out to its own PR-gate child."""
        return await _synthesize(build_campaign_notes_activity, "campaign")


@workflow.defn
class PlaybookDistillationWorkflow:
    """Run semantic playbook distillation durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Distil candidates, then fan each playbook note out to its own PR-gate child."""
        return await _synthesize(build_playbook_notes_activity, "playbook")


@workflow.defn
class OptimizationCampaignWorkflow:
    """Run episodic optimization-campaign grouping durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Group runs, then fan each optimization-campaign note out to its own PR-gate child."""
        return await _synthesize(build_optimization_notes_activity, "optimization")
