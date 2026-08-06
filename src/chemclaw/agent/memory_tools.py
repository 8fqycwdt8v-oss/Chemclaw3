"""Agent tools for the memory layers: capturing a confirmed answer, and recalling observations.

A confirmed or corrected answer from a chemist is evidence too. `record_confirmed_answer`
lets the agent capture such an exchange as an episodic `interaction` note and route it
through the **same** PR-gate as every other agent note (a human validates it before it
becomes trusted knowledge, D-005) — the fourth memory source, on the one shared write path.

`recall_observations` reads the ungated tier (D-161), and is a **separate tool on purpose**. An
observation is not evidence and must never arrive as a chunk in the evidence list: fusing the two
would make "what the record shows" and "what the agent noticed" the same kind of thing at the
moment of ranking, which is the distinction the human gate exists to preserve. Keeping it a
distinct call is what makes the separation structural rather than a naming convention.
"""

from chemclaw.agent.framing import frame_untrusted
from chemclaw.core.config import settings
from chemclaw.core.tool_registry import tool
from chemclaw.core.turn_signals import record_proposal
from chemclaw.kg.git_submitter import default_submitter
from chemclaw.memory.interaction import propose_confirmed_answer
from chemclaw.memory.observations import Observation, open_observations


@tool
async def record_confirmed_answer(
    interaction_id: str,
    question: str,
    answer: str,
    evidence_note_ids: list[str] | None = None,
) -> str:
    """Record a user-confirmed/corrected answer as an `interaction` note via the PR-gate.

    Call this only after the chemist has explicitly confirmed or corrected an answer, so the
    exchange becomes reusable knowledge. It is authored as `agent`, so it lands on a feature
    branch for human sign-off, never straight into the graph.

    Args:
        interaction_id: Stable, unique id for this exchange (becomes note `interaction-<id>`).
        question: The question that was answered.
        answer: The confirmed/corrected answer to preserve.
        evidence_note_ids: Ids of the notes the answer drew on, cited as `[[wikilinks]]`.

    Returns:
        The submitted PR reference.
    """
    reference = await propose_confirmed_answer(
        interaction_id, question, answer, evidence_note_ids, default_submitter()
    )
    # Surface the opened branch on the turn's stream, so the chemist sees their contribution land
    # instead of the PR-gate being visible only in a git host's UI (gap RCH-4).
    record_proposal(f"interaction-{interaction_id}", reference)
    return reference


@tool
async def recall_observations(limit: int = 0) -> list[Observation]:
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
    the system noticed and no one has confirmed. `support` is how many merged notes back it and
    `projects_seen` which projects it spans; both low means a thin reading, not a weak fact.

    Args:
        limit: How many to return, best-supported first. 0 uses the configured page size.

    Returns:
        Open observations with their statements, scope, supporting merged note ids, and projects.
    """
    if not settings.observations_enabled:
        return []
    # The one knowledge path with no human gate at all (D-161's ungated tier): a statement here was
    # mined from the corpus by a durable job and reaches the model without anyone having reviewed
    # it. Every *other* route from that corpus into context is framed — a note body at both of its
    # read sites, a chunk in `gather_evidence` — so leaving this one bare inverted the intended
    # ordering, giving the least-reviewed text the most direct reading.
    return [
        observation.model_copy(
            update={
                "statement": frame_untrusted(
                    observation.statement, note_id=observation.id or observation.scope
                )
            }
        )
        for observation in await open_observations(limit or None)
    ]
