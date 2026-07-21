"""Memory synthesis jobs (plan steps 5.3, 5.4, core) — chains/candidates → PR-gated notes.

The deterministic core of the two periodic background jobs: `synthesize_campaigns` turns
detected chains into `campaign` notes, and `distill_playbooks` turns cross-project candidates
into `playbook` notes — each proposed through the **same** PR-gate as every other agent note
(D-005), no new write path. The reaction set and the submitter are injected, so both run
in-memory in tests; `workflows.memory_jobs` wraps them as Temporal activities that read the
reactions from the ELN adapter. The factual note bodies are built here; the richer narrative /
distilled rule is the corresponding skill's judgment, layered on top.
"""

from eln.ord import OrdReaction
from kg.pr_gate import NoteSubmitter, propose_note
from memory.campaign import campaign_note_from_chain
from memory.chains import detect_chains
from memory.ids import stable_id
from memory.optimization import find_optimization_campaigns, optimization_campaign_note
from memory.playbook import PlaybookCandidate, find_playbook_candidates, playbook_note


async def synthesize_campaigns(reactions: list[OrdReaction], submitter: NoteSubmitter) -> list[str]:
    """Detect chains and propose a `campaign` note for each; return the PR references."""
    by_id = {r.reaction_id: r for r in reactions}
    refs: list[str] = []
    for chain in detect_chains(reactions):
        refs.append(await propose_note(campaign_note_from_chain(chain, by_id), submitter))
    return refs


async def distill_playbooks(reactions: list[OrdReaction], submitter: NoteSubmitter) -> list[str]:
    """Find cross-project candidates and propose a `playbook` note for each; return the refs."""
    by_id = {r.reaction_id: r for r in reactions}
    refs: list[str] = []
    for candidate in find_playbook_candidates(reactions):
        note = playbook_note(
            stable_id("playbook", candidate.reaction_ids),
            _summary(candidate, by_id),
            candidate.reaction_ids,
        )
        refs.append(await propose_note(note, submitter))
    return refs


async def synthesize_optimization_campaigns(
    reactions: list[OrdReaction], submitter: NoteSubmitter
) -> list[str]:
    """Group same-transformation runs and propose an `optimization-campaign` note for each."""
    by_id = {r.reaction_id: r for r in reactions}
    refs: list[str] = []
    for campaign in find_optimization_campaigns(reactions):
        note = optimization_campaign_note(
            stable_id("optimization", campaign.reaction_ids), campaign, by_id
        )
        refs.append(await propose_note(note, submitter))
    return refs


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
