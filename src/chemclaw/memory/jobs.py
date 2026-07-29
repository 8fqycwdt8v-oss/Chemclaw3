"""Memory synthesis jobs (plan steps 5.3, 5.4, core) — chains/candidates → PR-gated notes.

The deterministic core of the two periodic background jobs: `synthesize_campaigns` turns
detected chains into `campaign` notes, and `distill_playbooks` turns cross-project candidates
into `playbook` notes — each proposed through the **same** PR-gate as every other agent note
(D-005), no new write path. The reaction set and the submitter are injected, so both run
in-memory in tests; `chemclaw.durable.memory_jobs` wraps them as Temporal activities that read the
reactions from the ELN adapter. The factual note bodies are built here; the richer narrative /
distilled rule is the corresponding skill's judgment, layered on top.
"""

from datetime import date

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note
from chemclaw.kg.pr_gate import NoteSubmitter, propose_note
from chemclaw.memory.campaign import campaign_note_from_chain
from chemclaw.memory.chains import detect_chains
from chemclaw.memory.ids import stable_id
from chemclaw.memory.optimization import find_optimization_campaigns, optimization_campaign_note
from chemclaw.memory.playbook import PlaybookCandidate, find_playbook_candidates, playbook_note
from chemclaw.memory.supersede import supersede_updates


def build_campaign_notes(reactions: list[OrdReaction]) -> list[Note]:
    """Detect chains and build (not publish) one `campaign` note per chain, plus any supersedes.

    The deterministic half of campaign synthesis: it produces the notes but writes nothing, so it
    is reused both by the in-process `synthesize_campaigns` and by the durable workflow that fans
    each note out to its own PR-gate child (plan F10-D2) — one place decides *what* the notes are,
    the caller decides *how* they are written. "What" includes retiring the notes this run's
    clusters replaced (`_with_supersedes`), which is the one thing here that reads the corpus.
    """
    by_id = {r.reaction_id: r for r in reactions}
    return _with_supersedes(
        [campaign_note_from_chain(chain, by_id) for chain in detect_chains(reactions)]
    )


def build_playbook_notes(reactions: list[OrdReaction]) -> list[Note]:
    """Find cross-project candidates and build a `playbook` note each, plus any supersedes."""
    by_id = {r.reaction_id: r for r in reactions}
    return _with_supersedes(
        [
            playbook_note(
                stable_id("playbook", candidate.reaction_ids),
                _summary(candidate, by_id),
                candidate.reaction_ids,
            )
            for candidate in find_playbook_candidates(reactions)
        ]
    )


def build_optimization_notes(reactions: list[OrdReaction]) -> list[Note]:
    """Group same-transformation runs, building an optimization note each, plus any supersedes."""
    by_id = {r.reaction_id: r for r in reactions}
    return _with_supersedes(
        [
            optimization_campaign_note(
                stable_id("optimization", campaign.reaction_ids), campaign, by_id
            )
            for campaign in find_optimization_campaigns(reactions)
        ]
    )


def _with_supersedes(notes: list[Note]) -> list[Note]:
    """Append retirement updates for merged notes this run's clusters replaced (D-078).

    Applied inside each builder rather than at the two publish sites, so the in-process job and the
    durable activity both get it and neither can forget. A run that supersedes nothing — the normal
    case, where no cluster changed shape — returns `notes` unchanged; `load_notes` is cached behind
    a stat fingerprint, so the corpus read costs nothing on a repeat run.
    """
    if not notes:
        return notes
    existing = load_notes(settings.knowledge_path)
    return [*notes, *supersede_updates(notes, existing, date.today())]


async def _propose_all(notes: list[Note], submitter: NoteSubmitter) -> list[str]:
    """Propose each already-built note through the PR-gate; return the references (DRY)."""
    return [await propose_note(note, submitter) for note in notes]


async def synthesize_campaigns(reactions: list[OrdReaction], submitter: NoteSubmitter) -> list[str]:
    """Detect chains and propose a `campaign` note for each; return the PR references."""
    return await _propose_all(build_campaign_notes(reactions), submitter)


async def distill_playbooks(reactions: list[OrdReaction], submitter: NoteSubmitter) -> list[str]:
    """Find cross-project candidates and propose a `playbook` note for each; return the refs."""
    return await _propose_all(build_playbook_notes(reactions), submitter)


async def synthesize_optimization_campaigns(
    reactions: list[OrdReaction], submitter: NoteSubmitter
) -> list[str]:
    """Group same-transformation runs and propose an `optimization-campaign` note for each."""
    return await _propose_all(build_optimization_notes(reactions), submitter)


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
