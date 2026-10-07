"""Agent tools for the memory layers: capturing a confirmed answer, and recalling observations.

`record_confirmed_answer` captures a chemist's confirmed or corrected answer as an episodic
`interaction` note on the same write path as every other agent note.

`recall_observations` reads the ungated observation tier (D-161) and is a separate tool on purpose:
an observation is not evidence and must never arrive as a chunk in the evidence list.
"""

from pydantic import BaseModel, Field, computed_field

from chemclaw.agent.framing import frame_untrusted
from chemclaw.agent.tool_framing import defanged_payload
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_note_written
from chemclaw.kg.git_writer import default_writer
from chemclaw.memory.interaction import record_confirmed_answer_note
from chemclaw.memory.observations import (
    Observation,
    count_open_observations,
    open_observations,
)


@tool
async def record_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None = None,
    corrected_from: str = "",
) -> str:
    """Record a user-confirmed/corrected answer as an `interaction` note in the knowledge graph.

    Call this only after the chemist has explicitly confirmed or corrected an answer, so the
    exchange becomes reusable knowledge. It is authored as `agent` and is **readable by everyone as
    soon as this returns** — nobody reviews it, so record what the chemist actually said rather
    than a paraphrase that flatters the answer.

    Args:
        interaction_id: Stable, unique id for this exchange (becomes note `interaction-<id>`).
        question: The question that was answered.
        answer: The confirmed/corrected answer to preserve.
        evidence_note_ids: Ids of the notes the answer drew on, cited as `[[wikilinks]]`.
        corrected_from: What you had answered, when the chemist corrected you. Leave empty
            when they confirmed. A correction recorded without it reads as agreement, and the
            fact that you were wrong is the most useful thing in the note.

    Returns:
        A reference to what landed — the commit the note was recorded in.
    """
    reference = await record_confirmed_answer_note(
        interaction_id,
        question,
        answer,
        evidence_note_ids,
        default_writer(),
        corrected_from,
    )
    # Surface the opened branch on the turn's stream, so the chemist sees their contribution land
    # instead of the write being visible only in a git host's UI (gap RCH-4).
    record_note_written(f"interaction-{interaction_id}", reference)
    return reference


class ObservationRecall(BaseModel):
    """What the system has noticed, **and whether it was in a position to notice anything**.

    The bare `list[Observation]` this replaced collapsed three answers into one empty list, and the
    first of them is the shipped default: `observations_enabled` is **False** out of the box, and
    `recall_observations` opened `if not settings.observations_enabled: return []`. A disabled
    subsystem was indistinguishable from a corpus in which nothing had been noticed — on the one
    tier whose entire content is "the system noticed something".

    **This is a defect by this repository's own standard rather than by a new one.** The identical
    case is handled correctly one package over: `OutlierReport` carries
    `enabled=settings.calibration_enabled` and its verdict says "an empty one may mean the ledger
    is switched off entirely", because `calibration_enabled` defaults to False in the same way.

    `total_open` is the third answer — a page of ten out of fifteen and a tier holding exactly ten
    are the same list, and the tool's own `limit` docstring already told the model so in prose,
    which is the docstring-only pattern this field exists to end.
    """

    observations: list[Observation] = Field(default_factory=list)
    # Whether the tier is switched on at all in this deployment.
    enabled: bool = False
    # How many open observations exist, before the page bound; 0 while the tier is off, which the
    # verdict never reads as "none were found".
    total_open: int = Field(default=0, ge=0)
    # The page bound applied, which the store clamps to `observation_max_results`.
    limit_applied: int = Field(default=0, ge=0)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def verdict(self) -> str:
        """The one sentence to read before treating this list as a fact about the corpus.

        A `computed_field` so the sentence is serialized with the result.
        """
        standing = (
            "An observation is NOT evidence: it is a reading the system formed across projects "
            "and nobody has confirmed. Use it to decide where to look, never to support a claim."
        )
        if not self.enabled:
            return (
                "NOT RECORDED: the observations tier is switched off in this deployment, so "
                "nothing has ever been noticed and nothing was searched. An empty list here is "
                "NOT evidence that no cross-project pattern exists — say the tier is switched "
                f"off and that an operator must enable it. {standing}"
            )
        if self.total_open > len(self.observations):
            return (
                f"PARTIAL: {len(self.observations)} of {self.total_open} open observations are "
                f"shown, best-supported first (page bound {self.limit_applied}). {standing}"
            )
        if not self.observations:
            return (
                "NOTHING NOTICED: the tier is on and holds no open observation. Nothing has been "
                f"seen across enough projects to be worth noticing yet. {standing}"
            )
        return f"COMPLETE: every open observation is shown, best-supported first. {standing}"


def _readable(observation: Observation) -> Observation:
    """One observation with its statement framed and every other string in it neutralised.

    Every string field is mined from note bodies, so the whole payload is defanged
    (`agent/tool_framing.defanged_payload`) rather than a field list, which also covers fields added
    later. `statement`, the field citations are made against, is framed in an envelope instead, from
    the unescaped original because `frame_untrusted` defangs its own content. The store keeps the
    plain statement.
    """
    safe = defanged_payload(observation)
    return safe.model_copy(
        update={
            "statement": frame_untrusted(
                observation.statement, note_id=observation.id or "observation"
            )
        }
    )


@tool
async def recall_observations(limit: int = 0) -> ObservationRecall:
    """Recall cross-project patterns the system has noticed but that no human has validated.

    These are **not** knowledge. Each one is a reading the system formed by looking across
    projects — usually something the knowledge graph will never contain, because the rules that
    govern what becomes a note deliberately exclude it (a playbook may only be distilled from
    successes, so a transformation that has gone badly in three projects is nobody's note).

    Use them to decide **where to look**, never as the answer. An observation may point you at
    reactions worth gathering evidence on, or at a question worth asking the chemist. It may not
    support a claim: check `evidence_note_ids` and read those notes with `expand_note`, then make
    the claim from the notes.

    If an answer rests on an observation and nothing more, say so explicitly — that it is a pattern
    the system noticed and no one has confirmed. `support` is how many cited runs back it and
    `projects_seen` which projects it spans; both low means a thin reading, not a weak fact.

    **Read `verdict` before concluding anything from the length of this list.** The tier is off by
    default, so an empty list may mean it was never switched on, which is not "nothing was noticed".

    Args:
        limit: How many to return, best-supported first; 0 uses the configured page size and any
            request is clamped to it (`observation_max_results`, 10 by default).

    Returns:
        Open observations with their statements, scope, note ids and projects, plus `enabled`,
        `total_open`, and a `verdict`.
    """
    if not settings.observations_enabled:
        # No database is touched while the tier is off — the answer is about the deployment, and a
        # disabled tier must not cost a connection to say so.
        return ObservationRecall(enabled=False)
    # A statement is corpus-mined free text, so it is framed like any retrieved evidence chunk.
    found = await open_observations(limit or None)
    return ObservationRecall(
        enabled=True,
        total_open=await count_open_observations(),
        limit_applied=min(
            limit or settings.observation_max_results, settings.observation_max_results
        ),
        observations=[_readable(observation) for observation in found],
    )
