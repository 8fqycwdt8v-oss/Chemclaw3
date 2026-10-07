"""What the agent notices across projects, and where it is allowed to notice it from.

Two deterministic miners. Anti-feedback rule: support counts only `reaction-<id>` records,
transcriptions of experiments somebody ran, never notes the agent wrote (including `interaction`
notes, which are `created_by: agent`). So an observation cannot be corroborated by the agent's own
output; `mine_interactions` enforces this by counting only cited ids found in the reaction corpus.
The corpus miner asks what the record shows across projects that no note will state; the interaction
miner asks which answered questions already drew on more than one project.
"""

import logging

from chemclaw.core.config import settings
from chemclaw.ingest.eln.ord import OrdReaction, OutcomeClass
from chemclaw.kg.note import Note, cited_ids
from chemclaw.memory.observations import Observation
from chemclaw.memory.similarity import cluster_by_similarity, reaction_fingerprints

logger = logging.getLogger(__name__)


def _runs(count: int) -> str:
    """`1 run` / `3 runs` — a record a chemist reads as evidence must not say "1 runs"."""
    return f"{count} run" if count == 1 else f"{count} runs"


def mine_corpus(reactions: list[OrdReaction]) -> list[Observation]:
    """Cross-project transformation clusters the playbook bar discards, as observations.

    `find_playbook_candidates` keeps only successes, which drops the finding that a transformation
    has gone badly in several projects. An observation is not a recommendation, so it can hold that.

    Successes are dropped before fingerprinting, so the statement is scoped to the counted runs: it
    names failures and inconclusive runs separately and says any success lies outside the cluster. A
    cluster without a `FAILURE` is not emitted, since inconclusive runs carry no evidence about the
    chemistry; for the same reason only projects with a failure count toward the cross-project span,
    while inconclusive runs remain in the evidence. Deterministic, ordered by cluster anchor.
    """
    # Only outcomes the source stated; `is not SUCCESS` would count unassessed runs (`None`) as
    # failures.
    unsuccessful = [
        r for r in reactions if r.outcome_class in (OutcomeClass.FAILURE, OutcomeClass.INCONCLUSIVE)
    ]
    projected = [r for r in unsuccessful if r.project]
    # Name the field that emptied the pass: a binding without `outcome_class` (or `project`) yields
    # nothing forever, which otherwise looks like a quiet corpus.
    if reactions and not unsuccessful:
        logger.warning(
            "observation mining saw %d reaction(s) and none carried a stated failure or "
            "inconclusive outcome_class — if the source records outcomes, the binding is not "
            "mapping them, and this miner will never produce anything",
            len(reactions),
        )
    elif unsuccessful and not projected:
        logger.warning(
            "observation mining saw %d non-successful reaction(s) and none carried a project — "
            "cross-project recurrence cannot be counted without one",
            len(unsuccessful),
        )
    fingerprints = reaction_fingerprints(projected)
    project_of = {r.reaction_id: r.project for r in projected if r.reaction_id in fingerprints}
    outcome_of = {
        r.reaction_id: r.outcome_class for r in projected if r.reaction_id in fingerprints
    }

    observations: list[Observation] = []
    for cluster in cluster_by_similarity(fingerprints, settings.playbook_similarity_threshold):
        failures = [m for m in cluster if outcome_of.get(m) is OutcomeClass.FAILURE]
        if not failures:
            continue
        # Projects are taken over the failures only, matching the failure count in the statement; an
        # inconclusive run cannot be the second project that makes a recurrence.
        projects = sorted({p for member in failures if (p := project_of.get(member))})
        if len(projects) < 2:
            # One project repeating itself is episodic, which the campaign layer already covers.
            continue
        inconclusive = [m for m in cluster if outcome_of.get(m) is OutcomeClass.INCONCLUSIVE]
        aside = (
            f", with {_runs(len(inconclusive))} inconclusive (no evidence either way)"
            if inconclusive
            else ""
        )
        observations.append(
            Observation(
                statement=(
                    f"One transformation failed in {_runs(len(failures))} across "
                    f"{len(projects)} projects ({', '.join(projects)}){aside}. No successful run "
                    "is in this cluster: it is built from non-successful runs only, so any success "
                    "of the same transformation lies outside it and is not counted here. Nothing "
                    "proposes this as a playbook, because a playbook may only be distilled from "
                    "successes."
                ),
                scope=f"transformation:{min(cluster)}",
                evidence_note_ids=sorted(f"reaction-{member}" for member in cluster),
                projects_seen=projects,
                origin="corpus-mining",
            )
        )
    return observations


def mine_interactions(notes: list[Note], reactions: list[OrdReaction]) -> list[Observation]:
    """`interaction` notes whose own evidence already spans more than one project.

    When a chemist's question was answered from two projects' reactions, a transfer already happened
    in one conversation where no third project can find it. Support is the cited reactions, never
    the interaction note. Projects are derived through the reaction corpus, hence both arguments.
    Not clustered by topic: grouping questions by phrasing would mint findings out of wording.
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
                # Reactions only: the interaction note is agent-written and would let every
                # cross-project interaction count itself toward promotion. It is still named in
                # `scope` below.
                evidence_note_ids=sorted(c for c in cited if c in project_of),
                projects_seen=projects,
                origin="interaction",
            )
        )
    return observations
