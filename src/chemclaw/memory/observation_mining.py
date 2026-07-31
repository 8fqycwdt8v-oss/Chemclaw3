"""What the agent notices across projects, and where it is allowed to notice it from (D-161).

Two miners, both deterministic and both reading only what a human already merged. That second
constraint is not a preference — it falls out of the anti-feedback rule. Support counts distinct
*merged* notes, so a miner that produced observations backed by anything else would produce
observations that can never accumulate support and therefore never promote: a write-only log with
extra steps. Raw session transcripts are the concrete case this rules out.

Both miners answer the same question from opposite ends. The corpus miner asks "what does the
record show across projects that nothing will ever propose as a note?", and the interaction miner
asks "which questions did a chemist ask whose answer already crossed a project boundary?".
"""

import logging

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction, OutcomeClass
from chemclaw.kg.note import Note, cited_ids
from chemclaw.memory.observations import Observation
from chemclaw.memory.similarity import cluster_by_similarity, reaction_fingerprints

logger = logging.getLogger(__name__)


def mine_corpus(reactions: list[OrdReaction]) -> list[Observation]:
    """Cross-project transformation clusters the playbook bar discards, as observations.

    `find_playbook_candidates` keeps only `SUCCESS` runs (KNW-3), and correctly: a playbook is a
    rule worth *transferring*, so distilling a recurring failure into one would invert what the
    record says. But that filter throws away the finding, not just the recommendation — "this
    transformation has gone badly in two separate projects" is exactly the cross-project signal a
    process chemist wants, and today it reaches nobody.

    This tier can hold it because an observation is not a recommendation. The same cluster that
    would make an inadmissible playbook makes a legitimate thing to notice, and the statement says
    what the record shows rather than what to do about it.

    Deterministic: same corpus in, same observations out, ordered by cluster anchor.
    """
    unsuccessful = [r for r in reactions if r.outcome_class is not OutcomeClass.SUCCESS]
    projected = [r for r in unsuccessful if r.project]
    fingerprints = reaction_fingerprints(projected)
    project_of = {r.reaction_id: r.project for r in projected if r.reaction_id in fingerprints}
    outcome_of = {
        r.reaction_id: r.outcome_class for r in projected if r.reaction_id in fingerprints
    }

    observations: list[Observation] = []
    for cluster in cluster_by_similarity(fingerprints, settings.playbook_similarity_threshold):
        projects = sorted({p for member in cluster if (p := project_of.get(member))})
        if len(projects) < 2:
            # One project repeating itself is episodic, which the campaign layer already covers.
            continue
        outcomes = sorted({str(outcome_of[m]) for m in cluster if m in outcome_of})
        observations.append(
            Observation(
                statement=(
                    f"A transformation run in {len(projects)} projects "
                    f"({', '.join(projects)}) has {' and '.join(outcomes)} outcomes on every "
                    f"recorded attempt ({len(cluster)} runs). Nothing proposes this as a playbook, "
                    "because a playbook may only be distilled from successes."
                ),
                scope=f"transformation:{min(cluster)}",
                evidence_note_ids=sorted(f"reaction-{member}" for member in cluster),
                projects_seen=projects,
                origin="corpus-mining",
            )
        )
    return observations


def mine_interactions(notes: list[Note], reactions: list[OrdReaction]) -> list[Observation]:
    """Merged `interaction` notes whose own evidence already spans more than one project.

    The half of the tier that answers "what have chemists actually asked". A confirmed answer is
    already a merged note, so it is admissible support; what nothing reads today is the fact that
    *its evidence crossed a project boundary*. When a chemist's question was answered from two
    projects' reactions, the transfer already happened — in one conversation, invisibly, and
    nowhere that a third project can find it.

    Project attribution is derived, not stored: an `interaction` note cites its evidence as
    wikilinks, and the reaction corpus knows each reaction's project. That indirection is why this
    takes both arguments rather than reading notes alone.

    Deliberately not clustered by topic. Grouping questions by prose similarity would mint findings
    out of phrasing, and this repo has already ruled that out for hypotheses (D-162): a
    pattern-matched motive is indistinguishable downstream from testimony.
    """
    project_of = {f"reaction-{r.reaction_id}": r.project for r in reactions if r.project}

    observations: list[Observation] = []
    for note in sorted((n for n in notes if n.type == "interaction"), key=lambda n: n.id):
        cited = cited_ids(note.body)
        projects = sorted({p for note_id in cited if (p := project_of.get(note_id))})
        if len(projects) < 2:
            continue
        observations.append(
            Observation(
                statement=(
                    f"A question answered in one session drew on {len(projects)} projects "
                    f"({', '.join(projects)}); the transfer happened in that conversation and is "
                    f"recorded only in {note.id}."
                ),
                scope=f"interaction:{note.id}",
                # The interaction note *and* the evidence it cited: the interaction is what was
                # observed, the reactions are what make it cross-project. Both are merged notes.
                evidence_note_ids=sorted({note.id, *(c for c in cited if c in project_of)}),
                projects_seen=projects,
                origin="interaction",
            )
        )
    return observations
