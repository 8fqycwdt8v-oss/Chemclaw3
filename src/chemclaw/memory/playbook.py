"""Distil cross-project patterns into `playbook` candidates and notes.

The semantic layer: a transformation that recurs across projects, found by DRFP similarity and kept
only when it spans >=2 projects (one project's repetition is episodic). `playbook_note` requires
evidence citations. It is also used when an observation is promoted (`durable.observation_jobs`);
the note records which provenance it has, derived from its id.
"""

import logging
from datetime import date

from pydantic import BaseModel

from chemclaw.core.config import settings
from chemclaw.core.errors import ChemclawError
from chemclaw.ingest.eln.ord import OrdReaction, OutcomeClass
from chemclaw.kg.note import UNDISTILLED_TAG, Note
from chemclaw.memory.ids import is_cluster_anchored
from chemclaw.memory.similarity import cluster_by_similarity, reaction_fingerprints

logger = logging.getLogger(__name__)

# The two producers of a `playbook` note, recorded in its `source`: cross-project distillation of
# recurring reactions, or promotion of an accumulated observation.
SOURCE_DISTILLATION = "memory:cross-project-distillation"
SOURCE_PROMOTED_OBSERVATION = "memory:promoted-observation"


class PlaybookCandidate(BaseModel):
    """A group of similar reactions spanning >=2 projects — a playbook worth distilling."""

    reaction_ids: list[str]
    projects: list[str]


class PlaybookError(ChemclawError):
    """A playbook was built without the mandatory evidence references."""


def find_playbook_candidates(
    reactions: list[OrdReaction], threshold: float | None = None
) -> list[PlaybookCandidate]:
    """Group structurally similar reactions that recur across >=2 projects.

    Clustered by DRFP Tanimoto >= `threshold` (default `playbook_similarity_threshold`) via
    connected components, i.e. single-linkage (similarity is transitive). A cluster qualifies only
    with at least two distinct projects; reactions without a project are ignored. Deterministic and
    order-independent. Pairwise clustering is O(n^2), fine at current scale.
    """
    floor = threshold if threshold is not None else settings.playbook_similarity_threshold
    # Only stated successes evidence a playbook: a failed, inconclusive or unassessed (`None`) run
    # must not distil into a recommendation. A source recording no outcomes therefore yields no
    # playbooks.
    successes = [r for r in reactions if r.outcome_class is OutcomeClass.SUCCESS]
    # Only *projected*, fingerprintable reactions can evidence cross-project recurrence, so
    # scope to those before clustering (a degenerate reaction is dropped by the fingerprinter).
    projected = [r for r in successes if r.project]
    # Name the field when nothing qualifies: a binding without `outcome_class` (or `project`) would
    # otherwise never produce a playbook, silently.
    if reactions and not successes:
        logger.warning(
            "playbook mining saw %d reaction(s) and none carried a stated success outcome_class "
            "— if the source records outcomes, the binding is not mapping them, and no playbook "
            "will ever be distilled",
            len(reactions),
        )
    elif successes and not projected:
        logger.warning(
            "playbook mining saw %d successful reaction(s) and none carried a project — "
            "cross-project recurrence cannot be counted without one",
            len(successes),
        )
    fingerprints = reaction_fingerprints(projected)
    project_of = {r.reaction_id: r.project for r in projected if r.reaction_id in fingerprints}

    candidates: list[PlaybookCandidate] = []
    for cluster in cluster_by_similarity(fingerprints, floor):
        projects = sorted({p for r in cluster if (p := project_of.get(r))})
        if len(projects) >= 2:
            candidates.append(PlaybookCandidate(reaction_ids=cluster, projects=projects))
    return candidates


def playbook_note(
    note_id: str,
    summary: str,
    evidence_note_ids: list[str],
    *,
    minted_on: date | None = None,
    distilled: bool = True,
) -> Note:
    """Build an agent `playbook` note citing its evidence; reject one with no citations.

    `note_id` is the full note id (e.g. `chemclaw.memory.ids.stable_id("playbook", ...)`). Citations
    are the control, letting a chemist trace the rule to real experiments. `evidence_note_ids` are
    full note ids cited verbatim, so evidence need not be reactions.

    `source` is derived from the id via `is_cluster_anchored` rather than passed, so the stated
    provenance cannot disagree with the id. `minted_on` becomes `valid_from`, so the digest reports
    the playbook; it is a parameter because a Temporal workflow cannot read the wall clock (`None`
    leaves it open-ended). `distilled=False` marks a deterministic recurrence statement rather than
    a distilled rule and adds `UNDISTILLED_TAG`, which `kg.analytics` reports.
    """
    if not evidence_note_ids:
        raise PlaybookError(f"playbook {note_id!r} has no evidence references")
    citations = "\n".join(f"- [[{note_id}]]" for note_id in evidence_note_ids)
    body = f"{summary}\n\nEvidence:\n{citations}\n"
    return Note(
        id=note_id,
        type="playbook",
        created_by="agent",
        source=(
            SOURCE_DISTILLATION
            if is_cluster_anchored(note_id, evidence_note_ids)
            else SOURCE_PROMOTED_OBSERVATION
        ),
        tags=[] if distilled else [UNDISTILLED_TAG],
        body=body,
        valid_from=minted_on,
    )
