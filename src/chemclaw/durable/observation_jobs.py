"""The observations tier's durable half: mine and retire on a timer, promote on demand (D-161).

Shaped like the memory-synthesis jobs, on the core background queue over the same corpus. Mining
and retirement write rows the graph never sees and run on a Schedule: mining first so
`last_seen` is refreshed for everything still supported, retiring second so only what was not
re-observed ages out. Promotion writes a note into the graph, so it is started on demand.
"""

import asyncio
import logging
from datetime import date, timedelta

from pydantic import BaseModel
from temporalio import activity, workflow

with workflow.unsafe.imports_passed_through():
    from chemclaw.core.config import settings
    from chemclaw.durable.registry import durable_activity, durable_workflow
    from chemclaw.ingest.eln.compound import compound_dependencies
    from chemclaw.kg.git_writer import default_writer
    from chemclaw.kg.graph import load_notes
    from chemclaw.kg.note import Note
    from chemclaw.kg.record import record_note
    from chemclaw.memory.observation_mining import mine_corpus, mine_interactions
    from chemclaw.memory.observations import (
        Observation,
        promotable,
        promoted_observations,
        retire_stale,
        set_status,
    )
    from chemclaw.memory.observations import record as record_observations
    from chemclaw.memory.playbook import playbook_note
    from chemclaw.memory.supersede import retire_note

from chemclaw.durable.memory_jobs import read_corpus
from chemclaw.durable.publish import BAD_DATA_RETRY, note_publish_retry, queue_wait_timeout

logger = logging.getLogger(__name__)


class MiningReport(BaseModel):
    """What one mining pass saw and did — the facts the retirement step must not act without.

    `corpus_reactions` and `complete` exist because retirement ages rows out by `last_seen`, and
    `last_seen` is refreshed by mining: a corpus read that silently returned nothing (a
    misconfigured source that yields `[]` rather than raising) refreshed nothing, and after
    `observation_retire_after_days` of that the retirement step erased every open observation —
    with the tier's own health metric then reading "the miners produce noise", the exact wrong
    diagnosis. The workflow reads these fields to skip retirement when the pass saw no corpus, or
    a partial one.
    """

    recorded: int
    corpus_reactions: int
    complete: bool


@durable_activity("background")
@activity.defn
async def mine_observations_activity() -> MiningReport:
    """Run both miners over the merged corpus and upsert what they found. Returns the count.

    Upsert: an observation's id derives from its content, so a repeat finding accumulates evidence.
    Whether a row may also shrink is `read_corpus().complete`: on a partial read an absent member
    is not a retraction. An unparseable note just drops its observation, which then ages out
    through `retire_stale`.
    """
    corpus = await read_corpus()
    # Off the loop: `load_notes` is a synchronous full parse of the corpus.
    notes = await asyncio.to_thread(load_notes, settings.knowledge_path)
    found = [
        *mine_corpus(corpus.reactions),
        *mine_interactions(notes, corpus.reactions),
    ]
    recorded = await record_observations(found, complete=corpus.complete)
    logger.info(
        "observation mining recorded %d finding(s) over %d reaction(s)%s",
        recorded,
        len(corpus.reactions),
        "" if corpus.complete else " (partial read)",
    )
    return MiningReport(
        recorded=recorded, corpus_reactions=len(corpus.reactions), complete=corpus.complete
    )


@durable_activity("background")
@activity.defn
async def retire_stale_observations_activity() -> int:
    """Retire open observations the corpus has stopped supporting. Returns how many.

    A retirement rate approaching the mining rate says the miners produce noise.
    """
    retired = await retire_stale()
    if retired:
        logger.info("retired %d observation(s) nothing re-observed", retired)
    return retired


@durable_activity("background")
@activity.defn
async def promote_observations_activity() -> list[str]:
    """Record one `playbook` note per observation that has crossed both thresholds.

    Evidence count says the finding is not a coincidence; project count says it is not one team's
    habit. Nothing reviews a promotion, so the thresholds decide what is asserted at all. The note
    goes through the ordinary `record_note` and cites the reactions behind it, so a reader can
    check the evidence.
    """
    references: list[str] = []
    # Read from the store, so earlier promotions are visible and a retry sees the same view.
    earlier = [(row.id, frozenset(row.evidence_note_ids)) for row in await promoted_observations()]
    promoted: list[frozenset[str]] = [evidence for _, evidence in earlier]
    merged = {
        note.id: note for note in await asyncio.to_thread(load_notes, settings.knowledge_path)
    }
    # **Best-supported first, so a superset is seen before the subset it supersedes.**
    # `promotable()` orders by id, which is a hash and therefore arbitrary here.
    for observation in sorted(await promotable(), key=lambda o: o.support, reverse=True):
        evidence = frozenset(observation.evidence_note_ids)
        if any(evidence <= larger for larger in promoted):
            # A promoted row already rests on every note this one does (ids can move when a cluster
            # gains a member), so this one is retired rather than promoted twice.
            await set_status(observation.id, "retired")
            continue
        new_note_id = f"playbook-{observation.id.removeprefix('observation-')}"
        # This finding contains one promoted earlier: the earlier playbook is retired by the same
        # `record_note` call, so the graph never holds both.
        superseded: list[Note] = []
        for old_id, old_evidence in earlier:
            if old_evidence < evidence:
                predecessor = merged.get(f"playbook-{old_id.removeprefix('observation-')}")
                if predecessor is not None and predecessor.valid_to is None:
                    superseded.append(
                        retire_note(predecessor, [new_note_id], workflow_safe_today())
                    )
        note = playbook_note(
            new_note_id,
            _promotion_summary(observation),
            observation.evidence_note_ids,
            # The day the corpus first supported it, which is what `valid_from` means and what
            # makes the promotion reach a subscriber at all — see `playbook_note`.
            minted_on=workflow_safe_today(),
        )
        references.append(
            await record_note(
                note,
                default_writer(),
                dependencies=compound_dependencies(note),
                superseded=superseded,
            )
        )
        # Marked promoted only after the note write returns, so a failed write leaves it open for
        # retry.
        await set_status(observation.id, "promoted")
        promoted.append(evidence)
    return references


def workflow_safe_today() -> date:
    """Today, from an activity: activities may read wall clocks (only workflow code may not)."""
    return date.today()


def _promotion_summary(observation: Observation) -> str:
    """The distilled rule a promoted observation states, in the terms the note's reader needs.

    The support is described as transcribed runs, since a `reaction-<id>` citation names an
    unvetted `reaction_records` row; the caveat is addressed to whoever retrieves the note.
    """
    return (
        f"{observation.statement}\n\n"
        f"Distilled from an observation supported by {observation.support} cited runs "
        f"(unreviewed ELN transcriptions and merged interaction notes) across "
        f"{len(observation.projects_seen)} projects "
        f"({', '.join(observation.projects_seen)}). It was noticed by the observations tier, not "
        "asserted by it — no human has judged either the reading or the evidence behind it, so "
        "check the cited runs before relying on it."
    )


@durable_workflow("background")
# Deliberately left able to park: only the `observations` Schedule starts it (bounded by
# `schedule_run_timeout_seconds`), nothing polls it, and the next fire redoes a full re-mine.
@workflow.defn
class ObservationSynthesisWorkflow:
    """Mine, then retire — the observations tier's periodic half.

    Promotion is not here: it writes into the graph, so `ObservationPromotionWorkflow` is started
    on demand. Mining first refreshes `last_seen`; retiring second ages out only what was not
    re-observed.
    """

    @workflow.run
    async def run(self) -> None:
        """Refresh the tier against the current corpus, then age out what it no longer supports."""
        budget = timedelta(seconds=settings.memory_job_timeout_seconds)
        report = await workflow.execute_activity(
            mine_observations_activity,
            start_to_close_timeout=budget,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )
        if report.corpus_reactions == 0 or not report.complete:
            # A pass that saw no or a partial corpus refreshed nothing, so retiring on it would
            # erase the tier
            # because a source broke.
            workflow.logger.warning(
                "skipping observation retirement: the mining pass saw %d reaction(s), "
                "complete=%s — nothing was re-observed, so nothing may age out on it",
                report.corpus_reactions,
                report.complete,
            )
            return
        await workflow.execute_activity(
            retire_stale_observations_activity,
            start_to_close_timeout=budget,
            schedule_to_start_timeout=queue_wait_timeout(),
            retry_policy=BAD_DATA_RETRY,
        )


@durable_workflow("background")
# Declared: `synthesize_memory` starts this with no execution timeout and returns an id to poll, so
# a parked run would report `running` forever. Re-running is cheap and idempotent.
@workflow.defn(failure_exception_types=[Exception])
class ObservationPromotionWorkflow:
    """Promote the observations that have earned a playbook note — on demand, never on a timer.

    The one step of the tier that asserts anything, so every such note exists because somebody
    asked for it.
    """

    @workflow.run
    async def run(self) -> list[str]:
        """Write a note for each promotable observation; return the recorded note references."""
        return list(
            await workflow.execute_activity(
                promote_observations_activity,
                start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=note_publish_retry(),
            )
        )
