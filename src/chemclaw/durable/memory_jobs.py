"""Durable memory-synthesis jobs on the background queue.

Thin Temporal wrappers over `chemclaw.memory.jobs`: each reads the full reaction set from the
configured active ingest sources (the same set the ELN sync ingests) and records campaign or
playbook notes in the graph. Started on demand, never on a Schedule, so knowledge does not
arrive on a timer.
"""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.adapter import entry_window, fetch_was_truncated
    from chemclaw.ingest.eln.compound import compound_dependencies
    from chemclaw.ingest.eln.ord import OrdReaction, RecordTier
    from chemclaw.ingest.eln.warehouse.expr import pattern_budget
    from chemclaw.ingest.sources.registry import active_ingest_sources
    from chemclaw.kg.git_writer import default_writer
    from chemclaw.kg.record import record_note
    from chemclaw.memory.jobs import (
        SynthesisUnit,
        build_campaign_notes,
        build_optimization_notes,
        build_playbook_notes,
    )

from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.durable.orchestrator import fan_out
from chemclaw.durable.publish import (
    BAD_DATA_RETRY,
    fan_out_queue_wait_timeout,
    note_publish_retry,
    queue_wait_timeout,
)

logger = logging.getLogger(__name__)


class CorpusRead(BaseModel):
    """The memory corpus as one read: the reactions, **and whether that is all of them**.

    The second field exists because the first one alone is indistinguishable from a shrunken
    corpus, and one consumer acts on the difference. `memory.observations.record` replaces a
    stored observation's evidence when a pass is authoritative — which is only true if the pass
    saw everything — so a read that skipped entries must say so rather than let a partial view be
    written down as the complete record (the defect
    `D-2026-08-08-a-partial-answer-must-say-so` §6 fixes).

    `complete` is about *this* read, not about configuration: a source an operator has turned off
    is not part of the corpus, so a read without it is complete. What makes a read partial is an
    entry a source returned and this job could not map — the corpus holds a reaction the miner
    never saw. Honest limit: a source that silently returns fewer entries than it holds is
    invisible here, because nothing downstream of `fetch_new_entries` can know what it withheld.
    """

    reactions: list[OrdReaction]
    complete: bool


async def read_corpus() -> CorpusRead:
    """Read and map every reaction from the *configured active* ingest sources (the memory corpus).

    Uses the same source registry as the ELN sync, so memory and sync never disagree on which
    sources exist; every source maps to one canonical schema. Returns a `CorpusRead` so skipped
    entries are reported rather than silent.

    Each source is read to its end, not its first page: the fetch floor advances to the newest
    watermark seen until a page offers nothing new, and `seen` drops inclusive-boundary repeats. A
    source that reports rows still waiting but cannot hand them over makes the read incomplete.
    This reads every source in full once per miner activity.
    """
    reactions: list[OrdReaction] = []
    skipped = 0
    citation_only = 0
    unfinished: list[str] = []
    # One regex budget for the whole activity, since a page may be the entire corpus.
    # `expr.pattern_budget` is re-entrant, so per-entry `map_to_ord` calls share this deadline.
    with pattern_budget():
        # The bound is on what this activity holds, not on what it reads (see
        # `settings.memory_corpus_max_reactions`).
        cap = settings.memory_corpus_max_reactions
        capped = False
        for adapter in active_ingest_sources():
            # Per source, because entry ids are only unique within one.
            seen: set[str] = set()
            since = datetime.min.replace(tzinfo=UTC)
            while True:
                page = await adapter.fetch_new_entries(since)
                fresh = [raw for raw in page if raw.entry_id not in seen]
                if not fresh:
                    # Nothing new: the source is exhausted, or stuck on a page it cannot get past
                    # (what
                    # `fetch_was_truncated` still being true means).
                    if fetch_was_truncated(adapter):
                        unfinished.append(getattr(adapter, "name", type(adapter).__name__))
                    break
                seen.update(raw.entry_id for raw in fresh)
                for raw in fresh:
                    # Checked per entry, not per page, because a drop directory returns its whole
                    # corpus as one page.
                    if cap and len(reactions) >= cap:
                        capped = True
                        break
                    try:
                        reaction = adapter.map_to_ord(raw)
                    except ChemclawError as exc:
                        # A malformed entry is the sync's to report: skip and log it. Only
                        # `ChemclawError` (the bad-data
                        # contract) is caught, so unexpected errors surface.
                        logger.info("memory job skipped an unmappable ELN entry: %s", exc)
                        skipped += 1
                        continue
                    if reaction.tier is RecordTier.CITATION_ONLY:
                        # A citation-only record has no reaction SMILES for the structural miners,
                        # so it is left out and
                        # counted without making the read incomplete.
                        citation_only += 1
                        continue
                    reactions.append(reaction)
                if capped:
                    # Stop here and say so, rather than lose the pass or exhaust the worker's
                    # memory.
                    break
                if not fetch_was_truncated(adapter):
                    break
                since = max(
                    entry_window(raw.created_at, raw.modified_at, raw.retracted_at) for raw in fresh
                )
            if capped:
                break
        if citation_only:
            logger.info(
                "memory corpus read left out %d citation-only reaction(s): each names a species "
                "without its structure, and every memory miner works on structure",
                citation_only,
            )
        if skipped:
            logger.warning(
                "memory corpus read is incomplete: %d entr(y/ies) could not be mapped, so "
                "this pass "
                "saw %d reaction(s) and not the whole record",
                skipped,
                len(reactions),
            )
        if capped:
            logger.warning(
                "memory corpus read is incomplete: it stopped at "
                "memory_corpus_max_reactions=%d, so this pass saw %d reaction(s) and not the whole "
                "record. Raise the bound if the worker "
                "has the memory for it (~40 kB resident per reaction, measured) or narrow "
                "CHEMCLAW_DATA_SOURCES; the notes this pass writes are marked partial either way.",
                cap,
                len(reactions),
            )
        if unfinished:
            logger.warning(
                "memory corpus read is incomplete: %s still reported rows waiting after the "
                "last page "
                "it could serve, so this pass saw %d reaction(s) and not the whole record",
                ", ".join(unfinished),
                len(reactions),
            )
        return CorpusRead(
            reactions=reactions, complete=not skipped and not unfinished and not capped
        )

    # The builders (DRFP fingerprinting, O(n²) Tanimoto, NetworkX, a full corpus parse) run in a
    # worker thread so they do not stall every other activity and heartbeat on the shared loop.
    # `corpus_complete` travels into the builder because retirements land with the write and nothing
    # reviews them; `memory.jobs._units` says how it is used.


@durable_activity("background")
@activity.defn
async def build_campaign_notes_activity() -> list[SynthesisUnit]:
    """Detect reaction chains across the corpus and build (not publish) one campaign unit each."""
    corpus = await read_corpus()
    return await asyncio.to_thread(
        build_campaign_notes, corpus.reactions, corpus_complete=corpus.complete
    )


@durable_activity("background")
@activity.defn
async def build_playbook_notes_activity() -> list[SynthesisUnit]:
    """Distil cross-project candidates across the corpus, one playbook unit per candidate."""
    corpus = await read_corpus()
    return await asyncio.to_thread(
        build_playbook_notes, corpus.reactions, corpus_complete=corpus.complete
    )


@durable_activity("background")
@activity.defn
async def build_optimization_notes_activity() -> list[SynthesisUnit]:
    """Group same-transformation runs across the corpus, one optimization unit per group."""
    corpus = await read_corpus()
    return await asyncio.to_thread(
        build_optimization_notes, corpus.reactions, corpus_complete=corpus.complete
    )


@durable_activity("background")
@activity.defn
async def publish_memory_note_activity(unit: SynthesisUnit, actor: str = "") -> str:
    """Record one already-built memory note; return its reference (the fan-out publish step).

    Compound notes the note links are minted in the same submission, at the one write path every
    machine-written note passes through. `actor` stamps the ambient identity for the write, so its
    log lines carry the turn's actor; empty (system-triggered runs) stays empty rather than
    inventing an attribution.
    """
    note = unit.note
    if not actor:
        return await record_note(
            note,
            default_writer(),
            dependencies=compound_dependencies(note),
            superseded=unit.retirements,
        )
    token = set_current_identity(actor, frozenset())
    try:
        return await record_note(
            note,
            default_writer(),
            dependencies=compound_dependencies(note),
            superseded=unit.retirements,
        )
    finally:
        reset_current_identity(token)


@durable_workflow("background")
# Declared so a failing child is dropped by `fan_out` immediately rather than after its
# one-hour execution timeout, while the chemist who started the parent is polling it.
@workflow.defn(failure_exception_types=[Exception])
class PublishNoteWorkflow:
    """Record one memory note in the graph — the fan-out unit of a synthesis job.

    Each note is its own child workflow, so a poison note is isolated and dropped by the fan-out
    while the rest still land.
    """

    @workflow.run
    async def run(self, unit: SynthesisUnit) -> str:
        """Run the note-write activity for one unit with the bounded note-write retry."""
        return await workflow.execute_activity(
            publish_memory_note_activity,
            unit,
            start_to_close_timeout=timedelta(seconds=settings.note_write_timeout_seconds),
            # The fan-out queue wait rather than core's hour, so a note parked on an unserved queue
            # ends as
            # a named activity failure that `fan_out` logs and counts.
            schedule_to_start_timeout=fan_out_queue_wait_timeout(),
            retry_policy=note_publish_retry(),
        )


@durable_activity("background")
@activity.defn
async def resolve_notes_per_run() -> int:
    """Resolve the per-run note cap outside workflow code, as `resolve_fan_out_limit` does.

    The cap decides how many child workflows start, so reading settings in workflow code would make
    the command count depend on the replaying worker's config and break replay after a redeploy.
    """
    return settings.memory_max_notes_per_run


def _slice_for_this_run(
    units: list[SynthesisUnit], cap: int, id_prefix: str
) -> list[SynthesisUnit]:
    """Take at most `cap` notes, rotating the window on each daily run.

    A fixed `notes[:cap]` would never write the tail, since the builders are deterministic; the
    window rotates by the run's date, so consecutive runs cover the whole corpus. Sorted by id for
    stability; `workflow.now()` for replay. The window slices units, so a retirement always lands
    in the same run as its replacement.
    """
    if cap <= 0 or len(units) <= cap:
        return units
    ordered = sorted(units, key=lambda unit: unit.note.id)
    start = (workflow.now().date().toordinal() * cap) % len(ordered)
    window = (ordered + ordered)[start : start + cap]
    workflow.logger.warning(
        "%s synthesis capped at %d of %d notes this run (window from index %d); the rest are "
        "written on following runs — raise CHEMCLAW_MEMORY_MAX_NOTES_PER_RUN to widen it",
        id_prefix,
        cap,
        len(ordered),
        start,
    )
    return window


async def _synthesize(build_activity: Any, id_prefix: str) -> list[str]:
    """Build the notes in one activity, then fan each out to a `PublishNoteWorkflow` child.

    Shared by the three synthesis jobs, which differ only in the builder. What one run may write is
    capped, and what the cap drops is reported — see `_slice_for_this_run`.
    """
    units = await workflow.execute_activity(
        build_activity,
        start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
        schedule_to_start_timeout=queue_wait_timeout(),
        retry_policy=BAD_DATA_RETRY,
    )
    cap = await workflow.execute_local_activity(
        resolve_notes_per_run,
        # The generic short-activity budget, as `resolve_fan_out_limit` uses beside it.
        start_to_close_timeout=timedelta(seconds=settings.activity_timeout_seconds),
        retry_policy=BAD_DATA_RETRY,
    )
    return await fan_out(
        PublishNoteWorkflow, _slice_for_this_run(units, cap, id_prefix), id_prefix=id_prefix
    )


@durable_workflow("background")
# Declared: `synthesize_memory` starts this for a chemist with no execution timeout and returns an
# id to poll, so a parked run would report `running` forever. Failing loses nothing: the scan is
# re-requestable and re-written notes are byte-identical.
@workflow.defn(failure_exception_types=[Exception])
class CampaignSynthesisWorkflow:
    """Run episodic campaign synthesis durably; return the recorded note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Detect chains, then fan each campaign note out to its own note-write child."""
        return await _synthesize(build_campaign_notes_activity, "campaign")


@durable_workflow("background")
# Declared: `synthesize_memory` starts this for a chemist with no execution timeout and returns an
# id to poll, so a parked run would report `running` forever. Failing loses nothing: the scan is
# re-requestable and re-written notes are byte-identical.
@workflow.defn(failure_exception_types=[Exception])
class PlaybookDistillationWorkflow:
    """Run semantic playbook distillation durably; return the recorded note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Distil candidates, then fan each playbook note out to its own note-write child."""
        return await _synthesize(build_playbook_notes_activity, "playbook")


@durable_workflow("background")
# Declared: `synthesize_memory` starts this for a chemist with no execution timeout and returns an
# id to poll, so a parked run would report `running` forever. Failing loses nothing: the scan is
# re-requestable and re-written notes are byte-identical.
@workflow.defn(failure_exception_types=[Exception])
class OptimizationCampaignWorkflow:
    """Run episodic optimization-campaign grouping durably; return the recorded note refs."""

    @workflow.run
    async def run(self) -> list[str]:
        """Group runs, then fan each optimization-campaign note out to its own note-write child."""
        return await _synthesize(build_optimization_notes_activity, "optimization")
