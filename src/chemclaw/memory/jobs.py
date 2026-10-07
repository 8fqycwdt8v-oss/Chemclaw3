"""Memory synthesis builders: chains and candidates into agent notes.

`build_campaign_notes`, `build_playbook_notes` and `build_optimization_notes` build notes without
writing them; `durable/memory_jobs.py` publishes each through `kg/record.py` in its own write child,
so one failure does not take its siblings down. Reactions are injected, so builders run in memory in
tests. Bodies are factual and must stand alone: the narrative and distillation skills are applied
only on demand in a chat turn, never automatically.
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
    """One indivisible unit of a synthesis run: a note, plus the retirements it carries.

    The pairing is the point. A retirement and its replacement used to be independent notes in
    one flat list, and the per-run cap's rotating window could put them in different days' runs —
    so a reviewer could merge "retire `campaign-aaa`" while its replacement had not even been
    proposed yet, and the retired note's successor line named a note that did not exist. A unit
    travels through the fan-out whole: the retirement rides the replacement's write
    (`record_note`'s `superseded`) and is written *after* the successor it names, so the pair
    reaches a reader as the single decision it is.
    """

    note: Note
    retirements: list[Note] = Field(default_factory=list)


def build_campaign_notes(
    reactions: list[OrdReaction], *, corpus_complete: bool = True
) -> list[SynthesisUnit]:
    """Detect chains and build (not publish) one `campaign` unit per chain, retirements paired.

    Includes retiring the notes this run's clusters replaced (`_units`), the one part that reads the
    corpus.
    """
    by_id = {r.reaction_id: r for r in reactions}
    return _units(
        [
            campaign_note_from_chain(
                chain, by_id, minted_on=supported_from(chain.reaction_ids, by_id)
            )
            for chain in detect_chains(reactions)
        ],
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
                minted_on=supported_from(candidate.reaction_ids, by_id),
                # This producer finds a recurrence but does not generalise it;
                # `durable/observation_jobs.py` promotes a real claim and passes nothing here.
                distilled=False,
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
                stable_id("optimization", campaign.reaction_ids),
                campaign,
                by_id,
                minted_on=supported_from(campaign.reaction_ids, by_id),
            )
            for campaign in find_optimization_campaigns(reactions)
        ],
        corpus_complete=corpus_complete,
    )


#: What a note built from a truncated corpus read says about itself, in the body a chemist reads. A
#: read over `memory_corpus_max_reactions` yields partial knowledge, so it says so. Two risks: the
#: evidence may be a subset, and the id may differ from the full read's (if the anchor member was
#: dropped) with no retirement pass to supersede the old note.
PARTIAL_READ_CAVEAT = (
    "\n> Derived from an **incomplete** corpus read: this run hit "
    "`memory_corpus_max_reactions`, so the evidence cited above may be a subset of what the record "
    "holds, and this note's id may differ from the one the same cluster mints when the corpus is "
    "read whole. No note was retired on the strength of this run.\n"
)


def _marked_partial(note: Note) -> Note:
    """The note, saying in its own body that the read behind it was incomplete.

    Applied here, the one function both publish paths share, so every builder inherits it. A later
    complete run rewrites the note without the line.
    """
    return note.model_copy(update={"body": f"{note.body}{PARTIAL_READ_CAVEAT}"})


def supported_from(reaction_ids: list[str], reactions: dict[str, OrdReaction]) -> date | None:
    """The day the note's anchor run was performed, or `None` when the corpus cannot say.

    Becomes `valid_from`, which the digest reads as news. The note id hashes only the smallest
    member id (`memory/ids.py`), so the date must not move while the id holds; a `max` over members
    would re-notify on every new run, and a partial read could lower it silently. Two tiers, ordered
    by stability:

    1. the anchor's own `performed_at`, fixed while the anchor is; failing that,
    2. the earliest dated member, which later runs joining do not move.

    `None` only when no member is dated. Tier 2 can still move if an earlier-dated member joins.
    """
    if not reaction_ids:
        return None
    anchor = reactions.get(min(reaction_ids))
    if anchor is not None and anchor.performed_at is not None:
        return anchor.performed_at
    dated = [
        reaction.performed_at
        for reaction_id in reaction_ids
        if (reaction := reactions.get(reaction_id)) is not None
        and reaction.performed_at is not None
    ]
    return min(dated) if dated else None


def _units(notes: list[Note], *, corpus_complete: bool) -> list[SynthesisUnit]:
    """Pair each new note with the retirements it carries.

    Applied inside each builder so both publish paths get it. Each retirement from
    `supersede_updates` is assigned to its successor's unit, so a retirement never travels without
    the replacement it names.

    A partial corpus read marks its notes and retires nothing: missing members could make a cluster
    look gone, and retracting true notes on a partial read is wrong. A vanished cluster is never
    retired (it has no successor); "the corpus stopped supporting this" is a `failure-mode` note.
    """
    if not notes:
        return []
    if not corpus_complete:
        logger.warning(
            "memory synthesis skipped its retirement pass: the corpus read was incomplete, and "
            "retiring notes on a partial view retracts knowledge that may "
            "still be true"
        )
        return [SynthesisUnit(note=_marked_partial(note)) for note in notes]
    existing = load_notes(settings.knowledge_path)
    units = {note.id: SynthesisUnit(note=note) for note in notes}
    for retired in supersede_updates(notes, existing, date.today()):
        units[carrier_of(retired)].retirements.append(retired)
    return list(units.values())



def _summary(candidate: PlaybookCandidate, reactions: dict[str, OrdReaction]) -> str:
    """What the miner actually found: a recurrence, its projects and a representative reaction.

    States the finding only, with no instruction to distil it, since nothing applies distillation
    automatically and the body is read by retrieval and digests. That it is not yet a rule is
    carried by `UNDISTILLED_TAG`, which `kg.analytics` counts.
    """
    representative = reactions[candidate.reaction_ids[0]].reaction_smiles()
    return (
        f"This transformation recurs across {len(candidate.projects)} projects "
        f"({', '.join(candidate.projects)}). Representative reaction: `{representative}`."
    )
