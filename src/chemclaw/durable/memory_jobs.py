"""Durable memory-synthesis jobs (plan steps 5.3, 5.4) on the background queue.

Thin Temporal wrappers over `chemclaw.memory.jobs`: each activity reads the full reaction set from
the
configured active ingest sources (`chemclaw.ingest.sources.registry`, the same set the ELN sync
ingests — no new
store) and proposes campaign / playbook notes via the PR-gate. No new infrastructure — only new
note types produced by reusing existing pieces (Phase 5, G1).

**Started on demand, never on a Schedule** (D-2026-08-25). These three used to fire hourly and open
pull requests with nobody having asked, which is knowledge arriving on a timer. The mining is
unchanged — it is the *trigger* that moved — so a chemist or an agent workflow starts one when
there is a reason to look at what the corpus now supports.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.core.errors import ChemclawError
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.compound import compound_dependencies
    from chemclaw.ingest.eln.ord import OrdReaction
    from chemclaw.ingest.sources.registry import active_ingest_sources
    from chemclaw.kg.git_submitter import default_submitter
    from chemclaw.kg.note import Note
    from chemclaw.kg.pr_gate import propose_note
    from chemclaw.memory.jobs import (
        build_campaign_notes,
        build_optimization_notes,
        build_playbook_notes,
    )

from chemclaw.core.identity_context import reset_current_identity, set_current_identity
from chemclaw.durable.orchestrator import fan_out
from chemclaw.durable.publish import BAD_DATA_RETRY, note_publish_retry

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

    Reads the ingest halves of `settings.data_sources` (via `chemclaw.ingest.sources.registry`),
    the same source
    set the durable ELN sync ingests — so toggling `CHEMCLAW_DATA_SOURCES` changes what memory
    reasons over, and the two subsystems can never disagree on which sources exist (DUP-1). Every
    ingest half feeds the same canonical schema, so the memory layers reason over the union without
    knowing any source's shape. Adding a future source is one registry entry + one config token,
    not a change here (the "keep integrations dumb, put the reasoning above them" line).

    Returns a `CorpusRead` rather than the bare list so a skipped entry is a fact the caller can
    act on instead of a silence — see that model.
    """
    since = datetime.min.replace(tzinfo=UTC)
    reactions: list[OrdReaction] = []
    skipped = 0
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
                skipped += 1
                continue
    if skipped:
        logger.warning(
            "memory corpus read is incomplete: %d entr(y/ies) could not be mapped, so this pass "
            "saw %d reaction(s) and not the whole record",
            skipped,
            len(reactions),
        )
    return CorpusRead(reactions=reactions, complete=not skipped)


async def all_reactions() -> list[OrdReaction]:
    """The corpus as a plain list, for the three note builders that cannot act on completeness.

    They propose notes through the PR-gate, where a human reads what was built; nothing they do
    rewrites stored state on the strength of "this is everything". The observation miner does, and
    calls `read_corpus` directly.
    """
    return (await read_corpus()).reactions


@durable_activity("background")
@activity.defn
async def build_campaign_notes_activity() -> list[Note]:
    """Detect reaction chains across the corpus and build (not publish) one campaign note each."""
    return build_campaign_notes(await all_reactions())


@durable_activity("background")
@activity.defn
async def build_playbook_notes_activity() -> list[Note]:
    """Distil cross-project candidates across the corpus and build a playbook note per candidate."""
    return build_playbook_notes(await all_reactions())


@durable_activity("background")
@activity.defn
async def build_optimization_notes_activity() -> list[Note]:
    """Group same-transformation runs across the corpus and build an optimization note per group."""
    return build_optimization_notes(await all_reactions())


@durable_activity("background")
@activity.defn
async def publish_memory_note_activity(note: Note, actor: str = "") -> str:
    """PR-gate one already-built memory note; return its reference (the fan-out publish step).

    Any compound note the note links is minted into the same submission (STO-7). Applying that rule
    here, at the one gate every machine-written note passes through, is what keeps it out of each
    connector: a note author states the link, and the gate makes it resolve.

    `actor` stamps the ambient identity for the duration of the gate, because `propose_note` records
    a durable `NoteProposal` whose `actor` comes from `ambient_provenance()` — and no activity under
    `durable/` stamped one, so every proposal a durable job opened was recorded with `actor=""`.
    `list_note_proposals` scopes a non-reviewer's queue to `principal.oid` and `_visible_proposal`
    404s the detail view, so the chemist who launched the job could not see the PR opened on their
    behalf. That surface's own docstring gives exactly that as the reason it exists.

    Empty is the honest default and stays supported: the memory-synthesis jobs are system-triggered
    (a schedule, no user), and stamping a synthetic actor on them would make an unattributed
    proposal look attributed. Absent means absent.
    """
    if not actor:
        return await propose_note(
            note, default_submitter(), dependencies=compound_dependencies(note)
        )
    token = set_current_identity(actor, frozenset())
    try:
        return await propose_note(
            note, default_submitter(), dependencies=compound_dependencies(note)
        )
    finally:
        reset_current_identity(token)


@durable_workflow("background")
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


@durable_activity("background")
@activity.defn
async def resolve_notes_per_run() -> int:
    """Resolve the per-run note cap outside workflow code, as `resolve_fan_out_limit` does.

    `_slice_for_this_run`'s return value *is* the input list to `fan_out`, so the cap decides how
    many `StartChildWorkflow` commands the workflow emits. Reading live settings inside workflow
    code makes that a function of the replaying worker's config rather than of history: measured
    with `workflow.now()` pinned and the corpus fixed, `cap=25` emitted 25 children
    (campaign-000..024) and `cap=10` emitted 10 (campaign-000..009). A redeploy that lowers the
    value mid-fan-out therefore replays 10 starts against 25 recorded child-started
    events — a non-determinism error, which is a workflow *task* failure, which retries forever
    ignoring the retry policy and wedges the run (the trap D-093 documents).

    `orchestrator.py` states this rule and captures its own bound through a local activity; the line
    above it in the same function did not.
    """
    return settings.memory_max_notes_per_run


def _slice_for_this_run(notes: list[Note], cap: int, id_prefix: str) -> list[Note]:
    """Take at most `cap` notes, rotating the window on each daily run.

    These jobs rescan the whole corpus with no cursor and had no ceiling on what one run could
    propose. In practice they stay quiet — an id anchored on a cluster's smallest member reuses its
    branch, a byte-identical note produces no diff and no push — but nothing *bounded* them, and a
    large corpus import would open a PR per cluster on the first night.

    A plain cap would have replaced that with a worse bug. The builders are deterministic over the
    corpus, so `notes[:cap]` proposes the same first N every night and the tail is proposed *never*
    — knowledge silently lost, which is exactly what a "silent cap" means here. So the window
    rotates by the run's own date: consecutive daily runs cover consecutive slices and the whole
    corpus is reached within one cycle, after which every note is a no-op re-proposal.

    Sorted by id so the ordering is stable rather than incidental to build order, and
    `workflow.now()` rather than a wall clock because a workflow must replay identically. `cap` is
    passed in rather than read here for the same reason — see `resolve_notes_per_run`.
    """
    if cap <= 0 or len(notes) <= cap:
        return notes
    ordered = sorted(notes, key=lambda note: note.id)
    start = (workflow.now().date().toordinal() * cap) % len(ordered)
    window = (ordered + ordered)[start : start + cap]
    workflow.logger.warning(
        "%s synthesis capped at %d of %d notes this run (window from index %d); the rest are "
        "proposed on following runs — raise CHEMCLAW_MEMORY_MAX_NOTES_PER_RUN to widen it",
        id_prefix,
        cap,
        len(ordered),
        start,
    )
    return window


async def _synthesize(build_activity: Any, id_prefix: str) -> list[str]:
    """Build the notes in one activity, then fan each out to a `PublishNoteWorkflow` child (DRY).

    The three synthesis jobs differ only in which builder runs; the detect-then-fan-out topology is
    identical, so it lives here once. Detection reads the whole corpus (one activity); publishing is
    per-note and independent (one child each), so a slow or failing note never blocks the others.

    What one run may propose is capped, and what the cap drops is said out loud — see
    `_slice_for_this_run`.
    """
    notes = await workflow.execute_activity(
        build_activity,
        start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
        retry_policy=BAD_DATA_RETRY,
    )
    cap = await workflow.execute_local_activity(
        resolve_notes_per_run,
        # The generic short-activity budget, as `resolve_fan_out_limit` uses beside it.
        start_to_close_timeout=timedelta(seconds=settings.qm_activity_timeout_seconds),
        retry_policy=BAD_DATA_RETRY,
    )
    return await fan_out(
        PublishNoteWorkflow, _slice_for_this_run(notes, cap, id_prefix), id_prefix=id_prefix
    )


@durable_workflow("background")
@workflow.defn
class CampaignSynthesisWorkflow:
    """Run episodic campaign synthesis durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Detect chains, then fan each campaign note out to its own PR-gate child."""
        return await _synthesize(build_campaign_notes_activity, "campaign")


@durable_workflow("background")
@workflow.defn
class PlaybookDistillationWorkflow:
    """Run semantic playbook distillation durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Distil candidates, then fan each playbook note out to its own PR-gate child."""
        return await _synthesize(build_playbook_notes_activity, "playbook")


@durable_workflow("background")
@workflow.defn
class OptimizationCampaignWorkflow:
    """Run episodic optimization-campaign grouping durably; return the proposed note references."""

    @workflow.run
    async def run(self) -> list[str]:
        """Group runs, then fan each optimization-campaign note out to its own PR-gate child."""
        return await _synthesize(build_optimization_notes_activity, "optimization")
