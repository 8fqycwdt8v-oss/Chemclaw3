"""Distil cross-project patterns into `playbook` candidates + notes (plan step 5.4).

The semantic layer. A playbook captures a transformation that *recurs across projects* — the
signal is reaction-fingerprint similarity (DRFP, Phase 3) grouping reactions that are the same
kind of chemistry, kept only when the group spans >=2 distinct projects (a single project's
repetition is episodic, not a transferable rule). `find_playbook_candidates` is deterministic
(config threshold); `playbook_note` builds the note and **requires evidence references** — a
playbook with no citations is inadmissible (plan 5.4: Belegverweise verpflichtend). The
distilled rule's prose is the `playbook-distillation` skill's judgment, layered on this base.
"""

import networkx as nx
from pydantic import BaseModel

from chemclaw.config import settings
from chemclaw.errors import ChemclawError
from eln.ord import OrdReaction
from kg.note import Note
from mcp_servers.fpstore import FingerprintError, tanimoto
from mcp_servers.rxnfp.fingerprint import drfp_bitstring


class PlaybookCandidate(BaseModel):
    """A group of similar reactions spanning >=2 projects — a playbook worth distilling."""

    reaction_ids: list[str]
    projects: list[str]


class PlaybookError(ChemclawError):
    """A playbook was built without the mandatory evidence references (plan 5.4)."""


def find_playbook_candidates(
    reactions: list[OrdReaction], threshold: float | None = None
) -> list[PlaybookCandidate]:
    """Group structurally similar reactions that recur across >=2 projects.

    Reactions are clustered by DRFP Tanimoto >= `threshold` (default
    `playbook_similarity_threshold`) via connected components — **single-linkage**, so
    similarity is transitive (A~B, B~C groups A, B, C even if A and C are not directly
    similar). A cluster is a candidate only if its members carry at least two distinct
    projects. Reactions without a project cannot evidence cross-project recurrence and are
    ignored. Deterministic and order-independent (sorted output).

    Pairwise Tanimoto clustering is O(n²) in fingerprintable reactions — fine at today's
    scale, noticeable around ~10^4 reactions; the Postgres HNSW index (Phase 3) is the
    escape hatch when that day comes.
    """
    floor = threshold if threshold is not None else settings.playbook_similarity_threshold
    # A degenerate/unparseable reaction is dropped, never fatal: one bad reaction must not
    # abort distillation for the whole corpus (G4). Only projected, fingerprintable reactions
    # can evidence cross-project recurrence.
    fingerprints: dict[str, str] = {}
    project_of: dict[str, str] = {}
    for reaction in reactions:
        if not reaction.project:
            continue
        try:
            fingerprints[reaction.reaction_id] = drfp_bitstring(reaction.reaction_smiles())
        except FingerprintError:
            continue
        project_of[reaction.reaction_id] = reaction.project
    scoped_ids = list(fingerprints)

    graph: nx.Graph = nx.Graph()
    graph.add_nodes_from(scoped_ids)
    for i, a in enumerate(scoped_ids):
        for b in scoped_ids[i + 1 :]:
            if tanimoto(fingerprints[a], fingerprints[b]) >= floor:
                graph.add_edge(a, b)

    candidates: list[PlaybookCandidate] = []
    for component in nx.connected_components(graph):
        projects = sorted({project_of[r] for r in component})
        if len(projects) >= 2:
            candidates.append(PlaybookCandidate(reaction_ids=sorted(component), projects=projects))
    candidates.sort(key=lambda c: c.reaction_ids[0])
    return candidates


def playbook_note(note_id: str, summary: str, evidence_reaction_ids: list[str]) -> Note:
    """Build an agent `playbook` note citing its evidence; reject one with no citations.

    `note_id` is the full note id (e.g. from `memory.ids.stable_id("playbook", ...)`).
    `summary` is the distilled rule (from the `playbook-distillation` skill); every playbook
    must cite the reactions that evidence it via `[[reaction-<id>]]` wikilinks, so a reviewer
    (a process chemist) can trace the rule to real experiments before approving the merge.
    """
    if not evidence_reaction_ids:
        raise PlaybookError(f"playbook {note_id!r} has no evidence references")
    citations = "\n".join(f"- [[reaction-{rid}]]" for rid in evidence_reaction_ids)
    body = f"{summary}\n\nEvidence:\n{citations}\n"
    return Note(
        id=note_id,
        type="playbook",
        created_by="agent",
        source="memory:cross-project-distillation",
        body=body,
    )
