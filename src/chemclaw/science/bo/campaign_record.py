"""A BO campaign as a durable entity, and every suggestion made against it.

Why this exists: `suggest_next_experiment` is the path the conversational agent actually uses, and
it wrote nothing. The GP fit is milliseconds; the expensive part of an optimization is a chemist
and an agent jointly framing the problem out of scattered history — which decision variables
matter, which past runs are comparable enough to seed it — and that was discarded every turn, so
the same framing was rebuilt from scratch on the next question. At the same time
`knowledge/optimization-campaign/` notes come from retrospective DRFP clustering of already-ingested
reactions, with no identity link to any BO run: the system had a word for a campaign and no object
behind it.

Named `campaign_record` rather than `campaign` because `chemclaw.science.bo.campaign` is the
*engine's* ask/tell loop. That module runs a campaign; this one remembers it — the same split, for
the same reason, as `chemclaw.durable.job_record` beside the workflow that produces it.

**A campaign is identified by its problem.** `campaign_id` is a hash of the decision space and the
objective, so three refinements of one optimization accumulate against one campaign and nobody has
to "start" one first. Two chemists optimizing the same space converge on the same row, which is
correct: it is the same campaign.

This is the dependency-free half — models, both backends' contract, the in-memory backend, and the
two facades the connector tools call (`record_suggestion` writes, `read_campaign_thread` reads back
what a later session needs to continue). The psycopg half is `campaign_record_store.py`, imported
lazily:
the same split `chemclaw.kg.proposal` uses, so a process without Postgres never pulls a driver for
a store it will not use.
"""

import logging
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.config import settings
from chemclaw.core.ids import stable_hash
from chemclaw.science.bo.problem import Candidate, Observation, OptimizationProblem

logger = logging.getLogger(__name__)


# The fields that *are* the decision space. An **allowlist**, not `exclude={"descriptors"}`: with a
# denylist, any future field on a parameter silently forks every campaign id in the database, and
# the failure is invisible — a new id, an empty history, a chemist told their campaign is new.
# `descriptors` is absent because it is computed *from* `structures`, so a cache miss recomputing
# the same values, or a calculator upgrade shifting the sixth decimal, is not a new problem.
_SPACE_FIELDS = {"kind", "name", "lower", "upper", "categories", "structures"}


def campaign_id_for(problem: OptimizationProblem) -> str:
    """The stable id of the campaign this problem *is*.

    Derived from the decision space and the objective rather than minted per call, which is the
    whole difference between a campaign and a turn: asking twice about one optimization must reach
    one campaign, or the history a campaign exists to accumulate never accumulates.

    **Descriptors are excluded from the identity.** They are computed *from* the structures, so a
    cache miss recomputing them to the same values must not fork the campaign, and a calculator
    upgrade that shifts a descriptor in the sixth decimal is not a new optimization problem. The
    structures themselves *are* included: swapping a ligand for a different molecule is a different
    space, and should be a different campaign.
    """
    space = [
        parameter.model_dump(mode="json", include=_SPACE_FIELDS) for parameter in problem.parameters
    ]
    # The legacy key, always, spelled exactly as it was. A single-objective problem must hash to the
    # byte-identical payload it hashed to before `objectives` existed, or every recorded campaign
    # becomes unreachable — `read_campaign_thread` would tell each chemist their campaign is new.
    identity: dict[str, Any] = {
        "space": space,
        "objective": problem.objective.model_dump(mode="json"),
    }
    # Added only when they carry information, for the same reason.
    if len(problem.objectives) > 1:
        identity["objectives"] = [
            objective.model_dump(mode="json") for objective in problem.objectives
        ]
    # A constraint narrows the space, so a constrained problem is a different campaign from the
    # unconstrained one over the same bounds — the runs mean different things.
    if problem.constraints:
        identity["constraints"] = [
            constraint.model_dump(mode="json") for constraint in problem.constraints
        ]
    return f"campaign-{stable_hash(identity)}"


class Campaign(BaseModel):
    """One optimization problem, tracked across the turns that refine it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    problem: dict[str, Any] = Field(default_factory=dict)
    opened_by: str = ""
    created_at: datetime | None = None
    # What separates a campaign under active work from one abandoned in March.
    last_asked_at: datetime | None = None


class Suggestion(BaseModel):
    """One proposal made against a campaign, with the evidence it rested on.

    Both the candidates and the observations, because a suggestion is only interpretable against
    what was known when it was made: the same candidate proposed from three runs and from thirty
    means different things.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    candidates: list[Candidate] = Field(default_factory=list)
    observations: list[Observation] = Field(default_factory=list)
    # The calculations the decision space's descriptors came from, so a stale xTB run traces to the
    # suggestions drawn from it — what `calc_refs` was built for (D-133) and D-158 first made real.
    calc_refs: list[str] = Field(default_factory=list)
    actor: str = ""
    session_id: str = ""
    correlation_id: str = ""
    # Assigned by the store on insert.
    id: int = 0
    proposed_at: datetime | None = None


@runtime_checkable
class CampaignStore(Protocol):
    """Reads and writes campaigns and their suggestions, whichever backend holds them."""

    async def upsert_campaign(self, campaign: Campaign) -> None:
        """Record the campaign, or touch `last_asked_at` when it is already known."""
        ...

    async def add_suggestion(self, suggestion: Suggestion) -> int:
        """Append one proposal; return its id."""
        ...

    async def read_campaign(self, campaign_id: str) -> Campaign | None:
        """One campaign, or None when it has never been asked about."""
        ...

    async def suggestions_for(self, campaign_id: str, limit: int) -> list[Suggestion]:
        """A campaign's proposals, newest first."""
        ...


class InMemoryCampaignStore:
    """The same contract for a deployment whose durable records live in-process.

    Not a test double: it is the backend a `session_store="memory"` deployment gets, so the CLI and
    a dev stack accumulate a campaign's history for the life of the process rather than silently
    recording nothing. Every rule its Postgres sibling enforces holds here in the same terms — the
    campaign upserts on id and keeps its original opener, suggestions append and never overwrite.
    """

    def __init__(self) -> None:
        """Start with no campaigns and no suggestions."""
        self._campaigns: dict[str, Campaign] = {}
        self._suggestions: list[Suggestion] = []
        self._next_id = 1

    async def upsert_campaign(self, campaign: Campaign) -> None:
        """Record the campaign, keeping the original opener and refreshing `last_asked_at`."""
        now = datetime.now(UTC)
        existing = self._campaigns.get(campaign.campaign_id)
        if existing is None:
            self._campaigns[campaign.campaign_id] = campaign.model_copy(
                update={"created_at": now, "last_asked_at": now}
            )
            return
        # `opened_by` and `created_at` are deliberately not refreshed: whoever framed the campaign
        # framed it, and a later asker does not become its author.
        self._campaigns[campaign.campaign_id] = existing.model_copy(
            update={"problem": campaign.problem, "last_asked_at": now}
        )

    async def add_suggestion(self, suggestion: Suggestion) -> int:
        """Append one proposal; return its id."""
        new_id = self._next_id
        self._next_id += 1
        self._suggestions.append(
            suggestion.model_copy(update={"id": new_id, "proposed_at": datetime.now(UTC)})
        )
        return new_id

    async def read_campaign(self, campaign_id: str) -> Campaign | None:
        """One campaign, or None when it has never been asked about."""
        return self._campaigns.get(campaign_id)

    async def suggestions_for(self, campaign_id: str, limit: int) -> list[Suggestion]:
        """A campaign's proposals, newest first."""
        matches = [s for s in self._suggestions if s.campaign_id == campaign_id]
        return sorted(matches, key=lambda s: s.id, reverse=True)[:limit]


@cache
def campaign_store() -> CampaignStore:
    """The campaign store this deployment gets: durable where its other records are.

    Cached so the writer and any reader share one instance, which is what makes the in-memory
    backend accumulate rather than start empty on every call — the same reason
    `plan_approval_store` and `proposal_store` are cached.
    """
    if settings.session_store == "postgres":
        from chemclaw.science.bo.campaign_record_store import PostgresCampaignStore

        return PostgresCampaignStore()
    return InMemoryCampaignStore()


async def record_suggestion(
    problem: OptimizationProblem,
    candidates: list[Candidate],
    observations: list[Observation],
    calc_refs: list[str],
    provenance: tuple[str, str, str],
) -> str:
    """Persist one suggestion against the campaign its problem defines; return the campaign id.

    **Never raises.** The candidates are already computed and are what the chemist asked for, so a
    database blip must not turn a successful suggestion into a failed tool call — the trade
    `chemclaw.agent.audit` and `chemclaw.kg.proposal` both make, for the same reason: the record is
    about the thing, and losing it must not cost the thing.

    Returns the campaign id either way, because the id is a pure function of the problem: it is
    still the right handle for the agent to quote back, even on the turn where the write failed.
    """
    actor, session_id, correlation_id = provenance
    campaign_id = campaign_id_for(problem)
    try:
        store = campaign_store()
        await store.upsert_campaign(
            Campaign(
                campaign_id=campaign_id,
                objective=problem.objective.name,
                direction=problem.objective.direction,
                problem=problem.model_dump(mode="json"),
                opened_by=actor,
            )
        )
        await store.add_suggestion(
            Suggestion(
                campaign_id=campaign_id,
                candidates=candidates,
                observations=observations,
                calc_refs=calc_refs,
                actor=actor,
                session_id=session_id,
                correlation_id=correlation_id,
            )
        )
    except Exception:
        logger.warning("could not record BO suggestion for %s", campaign_id, exc_info=True)
    return campaign_id


class CampaignThread(BaseModel):
    """A recorded campaign in the shape a later session needs to pick it back up.

    The three parts of "where were we": the decision space as it was last framed, the observations
    the last suggestion rested on, and the candidates that suggestion proposed. That is the whole
    ask→observe→ask loop across sessions — without it, turn N+1 can only recover turn N's runs by
    re-reading them out of the chat transcript, which is why `suggest_next_experiment` telling the
    chemist to quote a `campaign_id` back was, until now, advice with nothing behind it.

    Only the **latest** suggestion's observations are carried, and that is complete rather than a
    truncation: each turn passes the campaign's whole run history, so the newest suggestion holds
    everything known when it was made.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    problem: OptimizationProblem
    observations: list[Observation] = Field(default_factory=list)
    last_candidates: list[Candidate] = Field(default_factory=list)
    opened_by: str = ""
    last_asked_at: datetime | None = None


async def read_campaign_thread(campaign_id: str) -> CampaignThread:
    """Read one campaign back, or raise saying why the id did not resolve.

    **Raises, where `record_suggestion` deliberately swallows.** The two are not symmetric: a write
    is incidental to a suggestion the chemist already has, so losing it must not cost them the
    suggestion — but a read *is* the whole request, and returning an empty thread on a failure
    would answer "this campaign has no history" to a question about a campaign that does.

    The not-found message names the hash property on purpose. `campaign_id_for` is a hash of the
    decision space, so the common cause of an unresolvable id is not a typo but a space that has
    since changed — one widened bound, one swapped ligand — which yields a *different* id rather
    than a conflicting record. That is the fact the caller needs to act on, and it is the reason
    resuming is a separate tool rather than an optional argument on `suggest_next_experiment`:
    given a stale id and a changed space, merging would silently seed a new campaign with
    observations from a different one, and neither the model nor the chemist would see it happen.
    """
    store = campaign_store()
    campaign = await store.read_campaign(campaign_id)
    if campaign is None:
        raise ValueError(
            f"no campaign is recorded under {campaign_id!r}. A campaign id is a hash of its "
            "decision space, so a space that has changed since — a widened bound, a swapped or "
            "added option — has a different id and no history under this one. Ask for a fresh "
            "suggestion over the current space instead of resuming."
        )
    latest = await store.suggestions_for(campaign_id, 1)
    return CampaignThread(
        campaign_id=campaign.campaign_id,
        objective=campaign.objective,
        direction=campaign.direction,
        problem=OptimizationProblem.model_validate(campaign.problem),
        observations=latest[0].observations if latest else [],
        last_candidates=latest[0].candidates if latest else [],
        opened_by=campaign.opened_by,
        last_asked_at=campaign.last_asked_at,
    )
