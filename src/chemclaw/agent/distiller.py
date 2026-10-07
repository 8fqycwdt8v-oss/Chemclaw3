"""Turn a recurring trajectory into a skill proposal, refusing evidence a skill produced itself.

Census in (`cli/trajectory_census.py`), `behaviour_proposals` out, with one guard between them:
a skill shapes the trajectories that follow its acceptance, so a session in which it was already
loaded (per `turn_costs.skills_loaded`) is not independent evidence for it. The guard is
session-granular because a skill read on one turn shapes every later turn of that conversation.

A proposal is a scaffold plus evidence, not model prose; model-written drafts go through
`propose_skill`. Runs on demand (`make distill`), never on a timer.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import yaml

from chemclaw.agent.behaviour_proposals import Proposal, content_hash, default_proposal_store
from chemclaw.agent.local_skills import validated_skill
from chemclaw.agent.skill_fingerprint import skill_fingerprint
from chemclaw.agent.skill_manifest import MAX_SKILL_DESCRIPTION_CHARS, MAX_SKILL_NAME_CHARS
from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.core.logging import log_event

logger = logging.getLogger(__name__)

#: How many distinct sessions a trajectory must recur in before it is worth proposing. Shared
#: with `trajectory_census`, and applied here to the independent count after the guard.
MIN_INDEPENDENT_SESSIONS = 2

#: What a tool name may contribute to a skill name. Everything else becomes a hyphen, because the
#: source is model-authored text that reaches YAML — see `candidate_name`.
_SAFE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Candidate:
    """One recurring trajectory that survived the guard, with the evidence that survived with it."""

    tools: tuple[str, ...]
    #: The sessions counted *after* the guard — never the census's raw count.
    sessions: tuple[str, ...]
    occurrences: int
    #: Sessions the guard removed, so a report can say what was discounted.
    self_confirming: tuple[str, ...]

    @property
    def name(self) -> str:
        """The skill name a proposal from this candidate takes.

        Derived from the trajectory so two runs over the same corpus propose the same name;
        proposals key on content, so a drifting name would multiply them.
        """
        return candidate_name(self.tools)


def candidate_name(tools: Sequence[str]) -> str:
    r"""One trajectory's skill name — sanitised, bounded, and collision-free when truncated.

    Tool names come from persisted model tool calls and are unvalidated, so they are sanitised
    before reaching YAML. The bound is `MAX_SKILL_NAME_LENGTH`, and a truncated name carries a
    digest of the whole tuple so two trajectories sharing a prefix do not collide.
    """
    slug = "-then-".join(_SAFE.sub("-", tool.lower()).strip("-") for tool in tools).strip("-")
    if not slug:
        slug = "unnamed-trajectory"
    if len(slug) <= MAX_SKILL_NAME_CHARS:
        return slug
    # `stable_hash` over the tuple rather than over the slug: two trajectories that sanitise to one
    # slug are still two trajectories, and the name has to say so.
    suffix = f"-{stable_hash(chr(31).join(tools))[:8]}"
    return slug[: MAX_SKILL_NAME_CHARS - len(suffix)].rstrip("-") + suffix


def independent_sessions(
    sessions: Sequence[str], skills_by_session: Mapping[str, frozenset[str]], candidate: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split a candidate's sessions into evidence and self-confirmation.

    The whole guard, with no I/O: a session in which the candidate skill was already loaded is not
    evidence for proposing it.

    Args:
        sessions: Every session the census saw this trajectory in.
        skills_by_session: The skill fingerprints `turn_costs.skills_loaded` records per session.
        candidate: The skill name a proposal would take.

    Returns:
        `(independent, discounted)` — the sessions that count, and the ones the guard removed.
    """
    independent: list[str] = []
    discounted: list[str] = []
    # Deduplicated: one session counted twice must not clear a bar meaning "two conversations".
    for session in dict.fromkeys(sessions):
        if skill_fingerprint(candidate) in skills_by_session.get(session, frozenset()):
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

    Takes the census's report so "recurring" has one definition.
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


def _inline(text: str) -> str:
    """Strip line breaks from one model-authored token.

    Used for both the Markdown body and the description, so there is one spelling of "safe".
    """
    return " ".join(text.split())


def _bounded(description: str) -> str:
    """Bound a description to `MAX_SKILL_DESCRIPTION_CHARS`, cutting on a word.

    `SkillManifest` refuses a longer one, so an unbounded proposal could never be accepted.
    """
    if len(description) <= MAX_SKILL_DESCRIPTION_CHARS:
        return description
    keep = description[: MAX_SKILL_DESCRIPTION_CHARS - 1].rsplit(" ", 1)[0]
    return f"{keep}\u2026"


def scaffold(candidate: Candidate) -> str:
    """The `SKILL.md` a candidate proposes — a scaffold and its evidence, not model prose.

    The body is a function of the trajectory alone: proposals key on its content hash, so growing
    counts in the body would reopen a rejected proposal. The moving evidence goes in `propose`'s
    rationale instead. The judgment section is left as an explicit gap for a person to write.
    """
    steps = "\n".join(
        f"{index}. `{_inline(tool)}`" for index, tool in enumerate(candidate.tools, start=1)
    )
    # Frontmatter values are emitted via `yaml.safe_dump`, not interpolated: they derive from
    # unvalidated tool names that could inject keys or break parsing.
    described = (
        f"The recurring sequence {' -> '.join(_inline(tool) for tool in candidate.tools)}, and "
        "the judgment about when to follow it."
    )
    frontmatter_block = yaml.safe_dump(
        {"name": candidate.name, "description": _bounded(described)},
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )
    return (
        "---\n"
        f"{frontmatter_block}"
        "---\n\n"
        f"# {' -> '.join(_inline(tool) for tool in candidate.tools)}\n\n"
        "**This is a scaffold, not finished judgment.** It was distilled from what recurred, which "
        "is a fact about tool calls rather than about chemistry. Keep it only if the judgment "
        "below is worth having; the sequence alone is not.\n\n"
        "## The steps that recurred\n\n"
        f"{steps}\n\n"
        "## When this applies, and when it does not\n\n"
        "_Not derivable from the trajectory — write it, or decline this._\n\n"
    )


async def propose(candidate: Candidate, actor: str) -> Proposal:
    """Put one candidate in the queue for its owner to decide.

    Idempotent by the queue's content-keyed rule: a re-run meets its own proposal or rejection.
    """
    body = scaffold(candidate)
    # Validate before filing, on the rules every other door applies, so an accepted proposal can
    # always be written.
    validated_skill(body, expected_name=candidate.name)
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

    More could never be accepted, since every accepted skill sits in every later turn's prefix.
    """
    strongest = sorted(found, key=lambda c: (-len(c.sessions), -c.occurrences, c.name))
    return strongest[: settings.agent_local_skills_max]
