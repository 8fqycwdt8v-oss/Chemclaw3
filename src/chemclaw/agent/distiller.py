"""Turn a recurring trajectory into a proposal, and refuse to count evidence it produced itself.

`D-2026-08-27-count-the-trajectories-before-building-the-distiller` set the order: measure the
corpus before building the miner. `chemclaw/cli/trajectory_census.py` is that instrument and it
already does the mining — recurring tool sequences, recurring failures, and the stated greenlight.
What it never had was a consumer, because there was nowhere for a proposal to go. There is now
(`D-2026-09-18-a-proposal-is-not-a-skill-and-a-route-is-not-a-tool`), so this module is the join:
census in, `behaviour_proposals` out, with one predicate between them.

**The predicate is the point, and it is why this module has a name rather than being four lines in
the CLI.** Nothing distilled may count evidence it itself produced. A skill, once accepted, is
injected into the prompt and *shapes the trajectories that follow* — so a sequence that recurs
because a skill is already teaching it is not independent evidence for proposing that skill. Left
unguarded, the loop is self-confirming by construction: propose, accept, observe the behaviour the
acceptance caused, propose again with a larger count.

**The guard needed a producer, and building it is what found that nothing recorded one.**
`chemclaw_skill_loads_total{skill}` says a skill was read and never in which turn; a Prometheus
counter is not a join key. `turn_costs.skills_loaded` is the record (`infra/sql/105_...`), written
from the same call the counter is taken on, on both tiers. Without it this predicate would have
been a function reading a column nobody writes, which is `map_to_hpc_identity` and the empty
`audit_events.agent` column exactly — a claim that a control exists.

**Session granularity, and that is stricter than per-turn rather than weaker than it.** A skill read
on a session's second turn shapes its third and its tenth, so asking "was this skill loaded in this
*turn*" would admit exactly the evidence the guard exists to exclude. The question is "was this
skill acting in this conversation", and the answer is the session's.

**What a distilled proposal contains is a scaffold and its evidence, not model prose.** The body
names the trajectory, the sessions it recurs in and the count, with the judgment left for a person
to write — because a chemist editing a scaffold they can check is a better bargain than prose a
model wrote about work it is summarising from tool names alone. Model-written drafts have their own
path and it is a better one: `propose_skill`, called in the turn where the procedure was worked out,
with the actual reasoning in context.

**On demand, never on a timer.** `CLAUDE.md`'s rule is that no Temporal Schedule opens a pull
request and knowledge never arrives on a timer; the campaign and playbook miners already work that
way, and this is one more of them. `make distill` is the caller.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from chemclaw.agent.behaviour_proposals import Proposal, content_hash, default_proposal_store
from chemclaw.core.config import settings
from chemclaw.core.logging import log_event

logger = logging.getLogger(__name__)

#: How many distinct sessions a trajectory must recur in before it is worth proposing.
#:
#: Not a new threshold: `trajectory_census.census` already only reports a class that appears in two
#: or more sessions, and this is the *independent* count after the guard has removed the sessions a
#: candidate skill was already acting in. Two is the census's own bar restated on the surviving
#: evidence, so the guard can only ever make a proposal harder to justify, never easier.
MIN_INDEPENDENT_SESSIONS = 2


@dataclass(frozen=True)
class Candidate:
    """One recurring trajectory that survived the guard, with the evidence that survived with it."""

    tools: tuple[str, ...]
    #: The sessions counted *after* the guard — never the census's raw count.
    sessions: tuple[str, ...]
    occurrences: int
    #: Sessions the census counted and the guard removed, so a report can say what was discounted
    #: rather than only what survived. An empty tuple is the common case and a full one is the
    #: finding: a trajectory that recurs only where a skill was already teaching it.
    self_confirming: tuple[str, ...]

    @property
    def name(self) -> str:
        """The skill name a proposal from this candidate takes.

        Derived from the trajectory rather than invented, because the name is the identity a person
        decides about and two runs of the miner over the same corpus must propose the same thing —
        `behaviour_proposals` keys on content, so a name that drifted would turn one idempotent
        proposal into an unbounded family of them.
        """
        return "-then-".join(tool.replace("_", "-") for tool in self.tools)[:64].rstrip("-")


def independent_sessions(
    sessions: Sequence[str], skills_by_session: Mapping[str, frozenset[str]], candidate: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a candidate's sessions into evidence and self-confirmation.

    **The whole guard, as one function with no I/O**, so it can be driven directly and so the rule
    is readable without a database: a session in which the candidate skill was already loaded is not
    evidence for proposing that skill.

    Args:
        sessions: Every session the census saw this trajectory in.
        skills_by_session: What `turn_costs.skills_loaded` says each session loaded.
        candidate: The skill name a proposal would take.

    Returns:
        `(independent, discounted)` — the sessions that count, and the ones the guard removed.
    """
    independent: list[str] = []
    discounted: list[str] = []
    for session in sessions:
        if candidate in skills_by_session.get(session, frozenset()):
            discounted.append(session)
        else:
            independent.append(session)
    return tuple(independent), tuple(discounted)


def candidates(
    report: Mapping[str, Any],
    occurrences_by_class: Mapping[tuple[str, ...], Sequence[str]],
    skills_by_session: Mapping[str, frozenset[str]],
) -> list[Candidate]:
    """Every recurring class that still has independent evidence behind it.

    Takes the census's own report rather than re-deriving recurrence, so there is one definition of
    "recurring" in this repository and the miner cannot disagree with the instrument that decided
    building it was worthwhile.
    """
    found: list[Candidate] = []
    for row in report.get("recurring_classes", []):
        tools = tuple(str(tool) for tool in row["tools"])
        sessions = occurrences_by_class.get(tools, ())
        candidate = Candidate(tools, (), int(row["occurrences"]), ())
        independent, discounted = independent_sessions(sessions, skills_by_session, candidate.name)
        if len(independent) < MIN_INDEPENDENT_SESSIONS:
            log_event(
                logger,
                "distiller.discounted",
                "%s recurs in %d session(s) but only %d independently of a skill already "
                "teaching it",
                candidate.name,
                len(sessions),
                len(independent),
                skill=candidate.name,
                sessions=len(sessions),
                independent=len(independent),
            )
            continue
        found.append(Candidate(tools, independent, int(row["occurrences"]), discounted))
    return found


def scaffold(candidate: Candidate) -> str:
    """The `SKILL.md` a candidate proposes — a scaffold and its evidence, not model prose.

    Deterministic, because two runs over one corpus must propose the same bytes:
    `behaviour_proposals` keys on the content hash, so a body carrying a timestamp or a
    re-ordered session list would turn one idempotent proposal into a new one every run, and a
    rejection would stop meaning anything.

    The `description` is what a model reads to decide whether to load this, so it names the
    trajectory rather than describing the file. The body names what nobody can derive — the judgment
    — as an explicit gap, because a scaffold that reads as finished is worse than one that asks.
    """
    steps = "\n".join(f"{index}. `{tool}`" for index, tool in enumerate(candidate.tools, start=1))
    sessions = "\n".join(f"- `{session}`" for session in sorted(candidate.sessions))
    return (
        "---\n"
        f"name: {candidate.name}\n"
        f"description: The sequence {' -> '.join(candidate.tools)}, which has recurred across "
        f"{len(candidate.sessions)} of this chemist's conversations.\n"
        "---\n\n"
        f"# {' -> '.join(candidate.tools)}\n\n"
        "**This is a scaffold, not finished judgment.** It was distilled from what recurred, which "
        "is a fact about tool calls rather than about chemistry. Keep it only if the judgment "
        "below is worth having; the sequence alone is not.\n\n"
        "## The steps that recurred\n\n"
        f"{steps}\n\n"
        "## When this applies, and when it does not\n\n"
        "_Not derivable from the trajectory — write it, or decline this._\n\n"
        "## What recurred\n\n"
        f"Observed {candidate.occurrences} time(s) across these conversations:\n\n"
        f"{sessions}\n"
    )


async def propose(candidate: Candidate, actor: str) -> Proposal:
    """Put one candidate in the queue for its owner to decide.

    Idempotent by the queue's own rule rather than by anything here: the same corpus distils the
    same bytes, so a second run meets its own proposal — and meets its own *rejection*, which is
    what stops a miner re-asking a question a person has answered.
    """
    body = scaffold(candidate)
    return await default_proposal_store().propose(
        Proposal(
            kind="skill",
            name=candidate.name,
            content_hash=content_hash(body),
            content=body,
            rationale=(
                f"{' -> '.join(candidate.tools)} recurred {candidate.occurrences} time(s) across "
                f"{len(candidate.sessions)} conversations"
                + (
                    f", with {len(candidate.self_confirming)} further conversation(s) discounted "
                    "because a skill of this name was already acting in them"
                    if candidate.self_confirming
                    else ""
                )
            ),
            actor=actor,
            session_id="",
            correlation_id="",
        )
    )


def bounded(found: Sequence[Candidate]) -> list[Candidate]:
    """At most `agent_local_skills_max` candidates, strongest first.

    A miner that proposed everything it found would fill a person's queue with a corpus's worth of
    scaffolds, and the cap it would be filling is the one that exists because every accepted skill
    sits in the prefix of every later turn. Proposing more than a person could accept is asking
    them to do the ranking the miner should have done.
    """
    strongest = sorted(found, key=lambda c: (-len(c.sessions), -c.occurrences, c.name))
    return strongest[: settings.agent_local_skills_max]
