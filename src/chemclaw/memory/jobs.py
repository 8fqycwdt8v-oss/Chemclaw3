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

from datetime import date

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction
from chemclaw.kg.graph import load_notes
from chemclaw.kg.note import Note
from chemclaw.memory.campaign import campaign_note_from_chain
from chemclaw.memory.chains import detect_chains
from chemclaw.memory.ids import stable_id
from chemclaw.memory.optimization import find_optimization_campaigns, optimization_campaign_note
from chemclaw.memory.playbook import PlaybookCandidate, find_playbook_candidates, playbook_note
from chemclaw.memory.supersede import supersede_updates


def build_campaign_notes(reactions: list[OrdReaction]) -> list[Note]:
    """Detect chains and build (not publish) one `campaign` note per chain, plus any supersedes.

    The deterministic half of campaign synthesis: it produces the notes but writes nothing, so the
    durable workflow that fans each note out to its own PR-gate child (plan F10-D2) decides *how*
    they are written while this decides *what* they are. "What" includes retiring the notes this
    run's clusters replaced (`_with_supersedes`), the one thing here that reads the corpus.
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
                [f"reaction-{rid}" for rid in candidate.reaction_ids],
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
