"""The observations tier's durable half: mine and retire on a timer, promote on demand (D-161).

Shaped exactly like the memory-synthesis jobs it sits beside — one workflow on the core background
queue, its activities reading the same corpus the ELN sync ingests — because D-019's constraint
still holds: a new knowledge layer adds no new infrastructure. The only new thing is a table.

The three steps are the tier's whole lifecycle, and they are split across two workflows because
only one of them costs a human anything. Mining and retirement write ungated rows and stay on a
Schedule, in that order — mining first, so `last_seen` is refreshed for everything the corpus still
supports, retiring second, so only what was *not* re-observed ages out. Promotion opens pull
requests, so it is started on demand (D-2026-08-25); a row cannot be retired in the same run that
would have promoted it, because no run does both any more.
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
    from chemclaw.kg.git_submitter import default_submitter
    from chemclaw.kg.graph import load_notes
    from chemclaw.kg.note import Note
    from chemclaw.kg.pr_gate import propose_note
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

    Upsert, not insert: an observation's id is derived from its content, so a finding seen again
    accumulates the evidence behind it instead of minting a near-duplicate row every night.

    Whether the row may also *shrink* is `read_corpus().complete`, passed straight through to
    `record`. Both miners derive every observation's evidence and projects from the reaction
    corpus — the interaction miner too, which attributes projects through `project_of` — so a
    partial read is exactly the case in which an absent member is not a retraction.

    A note `load_notes` skips does **not** make the pass partial, and that asymmetry is the point:
    an unparseable note drops its own observation from `found` entirely, so no row is rewritten —
    it simply stops being re-observed and ages out through `retire_stale`, which is the designed
    path. A skipped *reaction* is different: the observation is still emitted, with less behind it.
    """
    corpus = await read_corpus()
    # Off the loop: `load_notes` is a synchronous full parse of the corpus, and an async activity
    # shares its worker's event loop with every other activity on the queue. Same reason
    # `retrieval.retrievers` threads it.
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

    The instrumentation half of "if nothing ever promotes, delete the tier". A retirement rate that
    approaches the mining rate says the miners are producing noise, which is a fact about this
    feature that only a number can establish.
    """
    retired = await retire_stale()
    if retired:
        logger.info("retired %d observation(s) nothing re-observed", retired)
    return retired


@durable_activity("background")
@activity.defn
async def promote_observations_activity() -> list[str]:
    """Open one PR per observation that has crossed both thresholds; return the references.

    The human gate, moved. It does not disappear — a promoted observation becomes an ordinary
    agent-authored `playbook` note through the ordinary `propose_note`, reviewed by an ordinary
    human. There is deliberately no second write path into the graph (D-019/D-078): the tier's
    entire contribution is deciding *which* candidates are worth a reviewer's time.

    The note's body states the reading and cites the merged reactions behind it, so a reviewer
    judges the same evidence the threshold counted rather than taking the count on trust.
    """
    references: list[str] = []
    # The guard reads the *store*, not a pass-local list: a subset promoted last week must be as
    # visible to this pass as one promoted a minute ago, and an activity retry must re-derive the
    # same view instead of starting from empty — both holes the local list had
    # (`promoted_observations` carries the history).
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
            # A promoted row — this pass's or any earlier one's — rests on every note this one
            # does, and more: `memory.ids`-anchored ids move when a cluster gains a member that
            # sorts below the anchor, so the same finding can hold two rows over threshold.
            # Retired rather than promoted: the corpus superseded it, and it is not a finding a
            # reviewer should see twice.
            await set_status(observation.id, "retired")
            continue
        new_note_id = f"playbook-{observation.id.removeprefix('observation-')}"
        # The other direction: this finding *contains* one promoted earlier. Its merged playbook
        # — which nothing else can retire, because a promoted playbook's id is scope-anchored and
        # `supersede_updates`' lineage test correctly skips it — is retired in the same
        # submission, so the superset's PR carries the retirement as one reviewable decision.
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
        )
        references.append(
            await propose_note(
                note,
                default_submitter(),
                dependencies=compound_dependencies(note),
                superseded=superseded,
            )
        )
        # Marked promoted only after the PR exists. Marking first would lose the observation if the
        # submission failed: it would no longer be open, so nothing would ever retry it, and the
        # finding would be silently dropped at the one moment it had proved itself worth keeping.
        await set_status(observation.id, "promoted")
        promoted.append(evidence)
    return references


def workflow_safe_today() -> date:
    """Today, from an activity: activities may read wall clocks (only workflow code may not)."""
    return date.today()


def _promotion_summary(observation: Observation) -> str:
    """The distilled rule a promoted observation proposes, in the reviewer's terms.

    The support is described as what it is — transcribed runs, not "merged notes". Since
    D-2026-08-25 a `reaction-<id>` citation names an ungated `reaction_records` row, so the old
    wording told the reviewer a human had already signed off on evidence no human had seen; the
    reviewer at this gate is the *first* human in the loop, and the sentence has to say so.
    """
    return (
        f"{observation.statement}\n\n"
        f"Proposed from an observation supported by {observation.support} cited runs "
        f"(unreviewed ELN transcriptions and merged interaction notes) across "
        f"{len(observation.projects_seen)} projects "
        f"({', '.join(observation.projects_seen)}). It was noticed by the observations tier, not "
        "asserted by it — this PR is the first point at which a human is asked to judge either "
        "the reading or the evidence behind it."
    )


@durable_workflow("background")
@workflow.defn
class ObservationSynthesisWorkflow:
    """Mine, then retire — the observations tier's periodic half.

    **Promotion is not here, and that is the point** (D-2026-08-25). Mining and retirement write
    ungated rows: what the agent noticed, kept out of the knowledge graph, costing no review.
    Promotion opens a pull request, and a pull request nobody asked for is knowledge arriving on a
    timer — so it moved to `ObservationPromotionWorkflow`, which a chemist or an agent workflow
    starts when there is a reason to look at what has accumulated.

    The original ordering argument survives intact for the two steps that remain: mining first, so
    `last_seen` is refreshed for everything the corpus still supports, and retiring second, so only
    what was *not* re-observed ages out.
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
            # Retirement ages out what mining did not re-observe — so a pass that saw no corpus
            # (or a partial one) refreshed nothing, and retiring on its strength would erase the
            # tier because a *source* broke, not because the findings stopped holding
            # (`MiningReport` carries the incident).
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
@workflow.defn
class ObservationPromotionWorkflow:
    """Promote the observations that have earned a playbook note — on demand, never on a timer.

    The one step of the tier that costs a human something: it proposes PR-gated notes for the
    findings that crossed both promotion thresholds. Split out of the periodic workflow so that
    every note this system opens for review is opened because somebody asked for it.
    """

    @workflow.run
    async def run(self) -> list[str]:
        """Propose notes for promotable observations; return the references of the PRs opened."""
        return list(
            await workflow.execute_activity(
                promote_observations_activity,
                start_to_close_timeout=timedelta(seconds=settings.memory_job_timeout_seconds),
                schedule_to_start_timeout=queue_wait_timeout(),
                retry_policy=note_publish_retry(),
            )
        )
