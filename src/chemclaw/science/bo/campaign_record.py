"""A BO campaign as a durable entity, and every suggestion made against it.

Framing an optimization (which variables matter, which past runs seed it) is the expensive part, so
`suggest_next_experiment` records each suggestion against a campaign a later session can resume. The
durable `BoCampaignWorkflow` *runs* a campaign; this module remembers it.

**A campaign is identified by its problem**: `campaign_id` hashes the decision space and the
objective, so refinements of one optimization accumulate against one campaign without anyone
"starting" it.

This is the dependency-free half — models, the store contract, the in-memory backend, and the
facades the connector tools call (`record_suggestion`, `read_campaign_thread`). The psycopg half is
`campaign_record_store.py`, imported lazily.
"""

import json
import logging
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol, runtime_checkable

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.ids import canonical_text, stable_hash
from chemclaw.core.jsonb import STRICT_JSON
from chemclaw.science.bo.problem import (
    Candidate,
    CategoricalParameter,
    Constraint,
    ExcludeConstraint,
    Objective,
    Observation,
    OptimizationProblem,
    Parameter,
)

logger = logging.getLogger(__name__)


# The fields that *are* the decision space — an allowlist, so a new parameter field cannot
# silently fork every campaign id. `descriptors` is handled per parameter by `_space_of`.
_SPACE_FIELDS = {"kind", "name", "lower", "upper", "categories", "structures"}

# Fields deliberately not hashed. `tests/test_bo_campaign_record.py` asserts this set plus
# `_SPACE_FIELDS` covers every parameter field, so a new field forces an explicit decision.
_IDENTIFYING_EXCLUSIONS = {"descriptors"}

# Decimal places bounds and coefficients are rounded to before hashing: far below any stated
# precision, far above float noise from a model re-emitting `120.0`.
_BOUND_DECIMALS = 6


def _identity_labels(labels: list[str]) -> dict[str, str]:
    """Every label of one categorical, mapped to the string the identity payload uses for it.

    Case is chemistry in a SMILES (`C1CCNCC1` piperidine vs `c1ccncc1` pyridine), so labels are kept
    verbatim when *every* label in the space parses whole as a molecule (`require_molecule`, the
    strict parse); otherwise the list is names and each label folds via `canonical_text`. The space
    decides, not each label, because many lab codes (`CO`, `N`, `B`) are legal SMILES.

    A reduction that would merge two of the space's own labels is not applied to those labels (they
    key `structures`/`descriptors`, so a collision would drop an entry); the rest still fold. This
    does not raise, because the space is legal and raising would fail a computed suggestion.
    """
    reduced = _as_structures(labels) or {label: canonical_text(label) for label in labels}
    collisions = {value for value, count in Counter(reduced.values()).items() if count > 1}
    return {label: label if value in collisions else value for label, value in reduced.items()}


def _as_structures(labels: list[str]) -> dict[str, str] | None:
    """Every label as its canonical SMILES, or None when even one of them is not a structure."""
    try:
        return {label: require_canonical_smiles(label) for label in labels}
    except InvalidSmilesError:
        return None


def _space_of(parameter: Parameter) -> dict[str, Any]:
    """One parameter as the identity sees it, canonicalised.

    The caller is a model re-emitting a space it read back from `resume_campaign`, so re-casing or
    trailing spaces must not mint a new campaign: names fold (`canonical_text`), category labels are
    reduced by `_identity_labels`, and bounds are rounded to `_BOUND_DECIMALS`.

    Descriptors identify the space only when the caller supplied them directly (`structures is
    None`); when computed from `structures`, recomputation noise must not fork the campaign and the
    structures already identify it.
    """
    dumped = parameter.model_dump(mode="json", include=_SPACE_FIELDS)
    dumped["name"] = canonical_text(str(dumped["name"]))
    for bound in ("lower", "upper"):
        if dumped.get(bound) is not None:
            dumped[bound] = round(float(dumped[bound]), _BOUND_DECIMALS)
    if isinstance(parameter, CategoricalParameter):
        labels = _identity_labels(parameter.categories)
        # The set of choices is the space, not their order, so sort here — in the identity payload
        # only: the surrogate keeps the caller's order, since a bare `CategoricalInput` is ordinally
        # encoded.
        dumped["categories"] = sorted(labels.values())
        # `structures` and `descriptors` are keyed by the labels, so re-key through the one map
        # rather than reducing twice. The SMILES values are never touched.
        if parameter.structures is not None:
            dumped["structures"] = {
                labels[label]: smiles for label, smiles in parameter.structures.items()
            }
        elif parameter.descriptors is not None:
            # Added only when present, so a bare categorical keeps hashing to its existing payload.
            dumped["descriptors"] = {
                labels[label]: row
                for label, row in parameter.model_dump(mode="json", include={"descriptors"})[
                    "descriptors"
                ].items()
            }
    return dumped


def _canonical(constraint: Constraint, labels: Mapping[str, Mapping[str, str]]) -> dict[str, Any]:
    """One constraint as the identity sees it, in a form the caller's ordering cannot change.

    `base + acid <= 3` and `acid + base <= 3` are the same polytope and must be the same campaign.
    `labels` is each categorical parameter's own reduction, keyed by parameter name.
    """
    dumped = constraint.model_dump(mode="json")
    if isinstance(constraint, ExcludeConstraint):
        # Options are category labels, so re-key them through the parameter's map rather than
        # reducing again: an option list is a subset of the space and could reduce differently on
        # its own.
        dumped["pairs"] = sorted(
            [
                canonical_text(name),
                # The fallback is unreachable for a validated problem; it only keeps the lookup from
                # raising.
                sorted(labels.get(name, {}).get(option, option) for option in options),
            ]
            for name, options in zip(constraint.parameters, constraint.options, strict=True)
        )
        del dumped["parameters"], dumped["options"]
        return dumped
    dumped["terms"] = sorted(
        [canonical_text(name), round(float(coefficient), _BOUND_DECIMALS)]
        for name, coefficient in zip(constraint.parameters, constraint.coefficients, strict=True)
    )
    dumped["rhs"] = round(float(dumped["rhs"]), _BOUND_DECIMALS)
    del dumped["parameters"], dumped["coefficients"]
    return dumped


def _objective_identity(objective: Objective) -> dict[str, Any]:
    """One objective as the identity sees it: the name folded, the direction as declared.

    The direction is a closed set, so it is left alone.
    """
    dumped = objective.model_dump(mode="json")
    dumped["name"] = canonical_text(str(dumped["name"]))
    return dumped


def campaign_id_for(problem: OptimizationProblem) -> str:
    """The stable id of the campaign this problem *is*.

    Derived from the decision space and objectives, so asking twice about one optimization reaches
    one campaign. Model-authored names are reduced and bounds rounded (`_space_of`,
    `_objective_identity`), parameters and categories are sorted, and constraints canonicalized
    (`_canonical`), so re-emission cannot fork a campaign. `objectives` keep their order: the lead
    objective is privileged. Existing rows are re-keyed when this changes, by
    `chemclaw.cli.rekey_campaigns`.
    """
    space = sorted(
        (_space_of(parameter) for parameter in problem.parameters),
        key=lambda dumped: str(dumped["name"]),
    )
    # The legacy key, always: a single-objective problem must keep hashing to its existing payload
    # or every recorded campaign becomes unreachable.
    identity: dict[str, Any] = {
        "space": space,
        "objective": _objective_identity(problem.objective),
    }
    # Added only when they carry information, for the same reason.
    if len(problem.objectives) > 1:
        identity["objectives"] = [
            _objective_identity(objective) for objective in problem.objectives
        ]
    # A constraint narrows the space, so a constrained problem is a different campaign from the
    # unconstrained one over the same bounds — the runs mean different things.
    if problem.constraints:
        # One reduction per categorical parameter, derived once here and handed to every constraint
        # that names it, so a constraint cannot reduce a label set the space already reduced.
        labels = {
            parameter.name: _identity_labels(parameter.categories)
            for parameter in problem.parameters
            if isinstance(parameter, CategoricalParameter)
        }
        identity["constraints"] = sorted(
            (_canonical(constraint, labels) for constraint in problem.constraints),
            key=lambda dumped: json.dumps(dumped, sort_keys=True),
        )
    return f"campaign-{stable_hash(identity)}"


class Campaign(BaseModel):
    """One optimization problem, tracked across the turns that refine it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    # The **lead** objective only, for display and manual queries; further objectives live in
    # `problem["objectives"]`, which is authoritative. Identity hashes the whole list.
    objective: str = Field(min_length=1)
    # `str` rather than a `Literal`: this is built from a stored row on every resume, and an
    # unexpected value must not make the row unreadable. What is written is always one of the two.
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
    # The decision space **as it was when this was proposed**; `Campaign.problem` holds the latest.
    problem: dict[str, Any] = Field(default_factory=dict)
    # The durable run that produced this, empty for the inline tool. The idempotency key: a retried
    # activity must not append a duplicate suggestion.
    job_id: str = ""
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

    async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
        """Upsert the campaign and append its suggestion **atomically**.

        Returns the suggestion id and whether this call created the campaign. One method because the
        upsert replaces `problem`, so a failure between the writes would pair the new space with old
        evidence. The created flag comes from the write itself, since a prior read would race.
        """
        ...

    async def read_campaign(self, campaign_id: str) -> Campaign | None:
        """One campaign, or None when it has never been asked about."""
        ...

    async def suggestions_for(self, campaign_id: str, limit: int) -> list[Suggestion]:
        """A campaign's proposals, newest first."""
        ...


def _refuse_what_jsonb_would(campaign: Campaign, suggestion: Suggestion) -> None:
    """Raise the `ValueError` the Postgres sibling raises, on the same four payloads it wraps.

    Serializes with `STRICT_JSON` and discards the result, because `Jsonb` calls `dumps` lazily at
    execute time; the rule keeps one definition in `core.jsonb`.
    """
    for payload in (
        campaign.problem,
        [candidate.model_dump(mode="json") for candidate in suggestion.candidates],
        [observation.model_dump(mode="json") for observation in suggestion.observations],
        suggestion.problem,
    ):
        STRICT_JSON(payload)


class InMemoryCampaignStore:
    """The same contract for a deployment whose durable records live in-process.

    Not a test double: a `session_store="memory"` deployment gets it. It upserts campaigns on id
    keeping the original opener, appends suggestions, and refuses any payload `jsonb` would (e.g. a
    NaN). A differential test drives both stores through one scenario list.
    """

    def __init__(self) -> None:
        """Start with no campaigns and no suggestions."""
        self._campaigns: dict[str, Campaign] = {}
        self._suggestions: list[Suggestion] = []
        self._next_id = 1

    async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
        """Both writes or neither, as the Postgres sibling does.

        The `jsonb` refusal runs before either write, so a refused payload leaves the store
        untouched.

        Raises:
            ValueError: When a payload holds a non-finite float, which `jsonb` would reject.
        """
        _refuse_what_jsonb_would(campaign, suggestion)
        created = await self._upsert_campaign(campaign)
        return await self._add_suggestion(suggestion), created

    async def _upsert_campaign(self, campaign: Campaign) -> bool:
        """Record the campaign, keeping the original opener and refreshing `last_asked_at`.

        Returns whether the campaign did not exist before this call.
        """
        now = datetime.now(UTC)
        existing = self._campaigns.get(campaign.campaign_id)
        if existing is None:
            self._campaigns[campaign.campaign_id] = campaign.model_copy(
                update={"created_at": now, "last_asked_at": now}
            )
            return True
        # `opened_by` and `created_at` are deliberately not refreshed: whoever framed the campaign
        # framed it, and a later asker does not become its author.
        self._campaigns[campaign.campaign_id] = existing.model_copy(
            update={"problem": campaign.problem, "last_asked_at": now}
        )
        return False

    async def _add_suggestion(self, suggestion: Suggestion) -> int:
        """Append one proposal; return its id — or the existing id when a durable run is retried.

        The same idempotency rule `bo_suggestions_job_idx` enforces in Postgres. Keyed on the run,
        never on content: two identical asks are two history entries.
        """
        if suggestion.job_id:
            for existing in self._suggestions:
                if (existing.campaign_id, existing.job_id) == (
                    suggestion.campaign_id,
                    suggestion.job_id,
                ):
                    return existing.id
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

    Cached so writer and readers share one instance and the in-memory backend accumulates.
    """
    if settings.session_store == "postgres":
        from chemclaw.science.bo.campaign_record_store import PostgresCampaignStore

        return PostgresCampaignStore()
    return InMemoryCampaignStore()


# The failures that are the *database's*, not ours: a blip here must not fail a computed
# suggestion. A programming error must, which is why this is a tuple and not `Exception`.
_TRANSIENT_WRITE_FAILURES = (ConnectionError, OSError, TimeoutError, psycopg.Error)


class RecordedSuggestion(BaseModel):
    """What a write tells the caller: the campaign's handle, and whether it just came into being.

    The second half used to be a separate `campaign_is_known` read taken *before* the write, and
    that read could not answer the question it was asked. Two turns opening the same decision space
    concurrently both saw no campaign and both reported opening one; the upsert then serialized
    them, so exactly one was right and nothing could tell which. The write knows, so the write says.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    opened_new_campaign: bool = False


async def record_suggestion(
    problem: OptimizationProblem,
    candidates: list[Candidate],
    observations: list[Observation],
    calc_refs: list[str],
    provenance: tuple[str, str, str],
    job_id: str = "",
) -> "RecordedSuggestion":
    """Persist one suggestion against the campaign its problem defines.

    **Never raises on a database failure**, always raises on our own defects: the candidates are
    already computed, and losing the record must not cost the chemist the suggestion. Returns the
    campaign id either way (a pure function of the problem); `opened_new_campaign` is `False` when
    the write failed, since a failed write is ignorance.

    Args:
        problem: The decision space; identifies the campaign and is snapshotted onto the suggestion.
        candidates: The proposed point(s).
        observations: The evidence they were derived from.
        calc_refs: The calculation keys the descriptors came from.
        provenance: `(actor, session_id, correlation_id)`, as `connectors.caller` yields it.
        job_id: The durable run that produced this, empty for the inline tool; makes the write
            idempotent under activity retries.
    """
    actor, session_id, correlation_id = provenance
    campaign_id = campaign_id_for(problem)
    try:
        store = campaign_store()
        _, created = await store.record(
            Campaign(
                campaign_id=campaign_id,
                objective=problem.objective.name,
                direction=problem.objective.direction,
                problem=problem.model_dump(mode="json"),
                opened_by=actor,
            ),
            Suggestion(
                campaign_id=campaign_id,
                candidates=candidates,
                observations=observations,
                calc_refs=calc_refs,
                problem=problem.model_dump(mode="json"),
                job_id=job_id,
                actor=actor,
                session_id=session_id,
                correlation_id=correlation_id,
            ),
        )
    except _TRANSIENT_WRITE_FAILURES:
        # Narrow, and at WARNING: a database blip must not turn a computed suggestion into an error.
        # Anything else is a defect in this code and propagates.
        logger.warning("could not record BO suggestion for %s", campaign_id, exc_info=True)
        return RecordedSuggestion(campaign_id=campaign_id, opened_new_campaign=False)
    return RecordedSuggestion(campaign_id=campaign_id, opened_new_campaign=created)


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

    **There is deliberately no `opened_by` here, and the column it would have come from stays.**
    `bo_campaigns.opened_by` is an audit column and keeps doing that job. What it must not do is
    travel back out to a model and thence to a chemist as provenance, because on the inline path
    it holds whatever `X-Chemclaw-Actor` claimed — recorded as `unverified:<id>` precisely because
    nothing authenticated it. Rendering an unauthenticated self-assertion beside a campaign as
    "opened by" is the shape
    `D-2026-08-26-an-attribution-nothing-can-write-is-not-an-attribution` deleted elsewhere in this
    tree: an identity claim the system cannot stand behind is worse than no claim, because a reader
    has no way to see which one they are looking at. Who opened a campaign is answerable from the
    audit trail, by someone who can see whether the actor was verified.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    problem: OptimizationProblem
    observations: list[Observation] = Field(default_factory=list)
    last_candidates: list[Candidate] = Field(default_factory=list)
    last_asked_at: datetime | None = None


async def read_campaign_thread(campaign_id: str) -> CampaignThread:
    """Read one campaign back, or raise saying why the id did not resolve.

    Raises where `record_suggestion` swallows: a read is the whole request, and an empty thread
    would falsely answer "no history". The not-found message names the hash property because the
    usual cause is a changed decision space, which yields a different id; resuming is a separate
    tool so a stale id is never silently merged with a new space.
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
        last_asked_at=campaign.last_asked_at,
    )
