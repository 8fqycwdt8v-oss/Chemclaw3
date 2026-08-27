"""Memory synthesis jobs (plan steps 5.3, 5.4, core) — chains/candidates → PR-gated notes.

The deterministic core of the periodic background jobs: `build_campaign_notes` turns detected
chains into `campaign` notes, `build_playbook_notes` turns cross-project candidates into `playbook`
notes, and `build_optimization_notes` groups same-transformation runs — each then proposed through
the **same** PR-gate as every other agent note (D-005), no new write path.

**Building and publishing are separate, and only building lives here.** `durable/memory_jobs.py`
runs each builder as one activity and fans each note out to its own PR-gate child (F10-D2), so a
note that cannot be published does not take its siblings with it. The reaction set is injected, so
every builder runs in-memory in tests. The factual note bodies are built here; the richer narrative
/ distilled rule is the corresponding skill's judgment, layered on top.
"""

import logging
from datetime import date

from pydantic import BaseModel, Field

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note
from chemclaw.memory.campaign import campaign_note_from_chain
from chemclaw.memory.chains import detect_chains
from chemclaw.memory.ids import stable_id
from chemclaw.memory.optimization import find_optimization_campaigns, optimization_campaign_note
from chemclaw.memory.playbook import PlaybookCandidate, find_playbook_candidates, playbook_note
from chemclaw.memory.supersede import carrier_of, supersede_updates

logger = logging.getLogger(__name__)


class SynthesisUnit(BaseModel):
    """One reviewable unit of a synthesis run: a note, plus the retirements it carries.

    The pairing is the point. A retirement and its replacement used to be independent notes in
    one flat list, and the per-run cap's rotating window could put them in different days' runs —
    so a reviewer could merge "retire `campaign-aaa`" while its replacement had not even been
    proposed yet, and the retired note's successor line named a note that did not exist. A unit
    travels through the fan-out whole: the retirement rides the replacement's submission
    (`propose_note`'s `superseded`), lands in the same PR, and the pair merges as the single
    decision it is.
    """

    note: Note
    retirements: list[Note] = Field(default_factory=list)


def build_campaign_notes(
    reactions: list[OrdReaction], *, corpus_complete: bool = True
) -> list[SynthesisUnit]:
    """Detect chains and build (not publish) one `campaign` unit per chain, retirements paired.

    The deterministic half of campaign synthesis: it produces the notes but writes nothing, so the
    durable workflow that fans each unit out to its own PR-gate child (plan F10-D2) decides *how*
    they are written while this decides *what* they are. "What" includes retiring the notes this
    run's clusters replaced (`_units`), the one thing here that reads the corpus.
    """
    by_id = {r.reaction_id: r for r in reactions}
    return _units(
        [campaign_note_from_chain(chain, by_id) for chain in detect_chains(reactions)],
        corpus_complete=corpus_complete,
    )


def build_playbook_notes(
    reactions: list[OrdReaction], *, corpus_complete: bool = True
) -> list[SynthesisUnit]:
    """Find cross-project candidates and build a `playbook` unit each, retirements paired."""
    by_id = {r.reaction_id: r for r in reactions}
    return _units(
        [
            playbook_note(
                stable_id("playbook", candidate.reaction_ids),
                _summary(candidate, by_id),
                [f"reaction-{rid}" for rid in candidate.reaction_ids],
            )
            for candidate in find_playbook_candidates(reactions)
        ],
        corpus_complete=corpus_complete,
    )


def build_optimization_notes(
    reactions: list[OrdReaction], *, corpus_complete: bool = True
) -> list[SynthesisUnit]:
    """Group same-transformation runs into optimization units, retirements paired."""
    by_id = {r.reaction_id: r for r in reactions}
    return _units(
        [
            optimization_campaign_note(
                stable_id("optimization", campaign.reaction_ids), campaign, by_id
            )
            for campaign in find_optimization_campaigns(reactions)
        ],
        corpus_complete=corpus_complete,
    )


def _units(notes: list[Note], *, corpus_complete: bool) -> list[SynthesisUnit]:
    """Pair each new note with the retirements it carries (D-078, retirement half).

    Applied inside each builder rather than at the two publish sites, so the in-process job and
    the durable activity both get it and neither can forget. Every retirement
    `supersede_updates` produces has at least one successor among `notes` (that is its
    definition), and it is assigned to that successor's unit so the pair travels together — the
    cap's rotating window can never again split "retire A" from the replacement it names.

    **A partial corpus read builds notes but retires nothing.** A read that skipped entries can
    legitimately stop minting a cluster's id — the cluster's members were in the skipped part —
    and retiring merged knowledge on the strength of a read that saw less than the record would
    propose retracting notes that are still true, gated only by a reviewer with no way to know
    the read was partial. Said out loud, because a run that quietly skips its retirement half
    looks identical to one with nothing to retire.

    A run producing zero notes retires nothing by construction — `supersede_updates` only retires
    a note some new note *replaced*, and a vanished cluster has no successor to point at. That is
    a stated limit, not an oversight: a retirement with no successor would have nothing to write
    in its "superseded by" line, and "the corpus stopped supporting this" is `failure-mode` /
    reviewer territory, not a mechanical retraction.
    """
    if not notes:
        return []
    if not corpus_complete:
        logger.warning(
            "memory synthesis skipped its retirement pass: the corpus read was incomplete, and "
            "retiring merged notes on a partial view proposes retracting knowledge that may "
            "still be true"
        )
        return [SynthesisUnit(note=note) for note in notes]
    existing = load_notes(settings.knowledge_path)
    units = {note.id: SynthesisUnit(note=note) for note in notes}
    for retired in supersede_updates(notes, existing, date.today()):
        units[carrier_of(retired)].retirements.append(retired)
    return list(units.values())


# Three `synthesize_*`/`distill_*` coroutines and their shared `_propose_all` stood here: build the
# notes, then publish them all in one pass. Nothing ran them. F10-D2 split each job into a builder
# and a durable fan-out — `durable/memory_jobs.py` imports only `build_campaign_notes`,
# `build_playbook_notes` and `build_optimization_notes` and gives each note its own PR-gate child,
# so a note that fails to publish no longer takes its siblings with it — and the old whole-batch
# publishers were left behind with the tests that exercised them.
#
# One of those tests asserted a *parity* between the live builder and the dead publisher, which is
# the strongest reason to delete rather than keep: it read as coverage of the shipped path while
# pinning a path nothing runs, and it would have gone on passing after the live half broke. The
# tests now build and propose the way the durable job does.


def _summary(candidate: PlaybookCandidate, reactions: dict[str, OrdReaction]) -> str:
    """A factual, deterministic placeholder summary; the skill distils the real rule.

    States what is objectively true — a transformation recurring across the named projects,
    with a representative reaction — so even before the LLM refines it the note is honest.
    """
    representative = reactions[candidate.reaction_ids[0]].reaction_smiles()
    return (
        f"Transformation recurring across {len(candidate.projects)} projects "
        f"({', '.join(candidate.projects)}); representative reaction `{representative}`. "
        f"Distil the transferable rule and conditions from the cited evidence."
    )
