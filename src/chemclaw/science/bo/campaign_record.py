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

import json
import logging
from collections import Counter
from datetime import UTC, datetime
from functools import cache
from typing import Any, Protocol, runtime_checkable

import psycopg
from pydantic import BaseModel, ConfigDict, Field

from chemclaw.core.chem import InvalidSmilesError, require_canonical_smiles
from chemclaw.core.config import settings
from chemclaw.core.ids import canonical_text, stable_hash
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


# The fields that *are* the decision space. An **allowlist**, not `exclude={"descriptors"}`: with a
# denylist, any future field on a parameter silently forks every campaign id in the database, and
# the failure is invisible — a new id, an empty history, a chemist told their campaign is new.
# `descriptors` is handled per parameter by `_space_of`, not here, because whether it identifies the
# space depends on where it came from.
_SPACE_FIELDS = {"kind", "name", "lower", "upper", "categories", "structures"}

# An allowlist inverts the denylist's failure rather than removing it: a field added to a parameter
# later is silently *not* hashed, so two different spaces would share one id and one history.
# `tests/test_bo_campaign_record.py` asserts this set stays exhaustive, so a new field forces an
# explicit decision instead of a silent one.
_IDENTIFYING_EXCLUSIONS = {"descriptors"}

# Decimal places a bound or a coefficient is rounded to before it is hashed. 6 is far below any
# precision a chemist states a range in (a temperature range is degrees, an equivalence is
# hundredths) and far above the float noise a model re-emitting `120.0` introduces.
_BOUND_DECIMALS = 6


def _identity_label(label: str) -> str:
    """One category label reduced to what it means: by RDKit when it is a molecule, else as text.

    `canonical_text` folds case, and **case is chemistry in a SMILES**: `C1CCNCC1` is piperidine,
    `c1ccncc1` is pyridine, and the two casefold to one string. That is not a corner of the
    vocabulary, it is the one shipped decision space — `objectives.molecule_library_problem` makes
    the canonical SMILES *itself* the category label. Measured before this: two chemists screening
    those two libraries got one campaign id, the second was told their campaign was not new, its
    space overwrote the first's, and `read_campaign_thread` handed whoever resumed either one the
    other's observations.

    So a label is reduced by the rule its own data type has. Both rules do the same job — drop what
    a model varies freely when it re-types a space it just read, keep what the value means — and on
    a structure RDKit's is the stronger of the two: `OCC` and `CCO` reach one campaign the way `THF`
    and `thf` do.

    **The strict parse, and deliberately not the lenient one `connectors.bo.knowledge._molecule_in`
    uses on the same question.** That caller asks "should this level be printed as a structure" and
    errs towards yes, because a wrong yes costs a pair of backticks. Here a wrong yes costs a *split
    campaign*, so the errors are not interchangeable: a label RDKit reads only a prefix of — a level
    a chemist named `CN=[N+]=[N-] (2 equiv)`, free-form labels being what they are — is prose
    describing a molecule rather than a molecule, and must keep folding. Only a string RDKit reads
    whole is a structure, which is what `require_molecule` means.
    """
    try:
        return require_canonical_smiles(label)
    except InvalidSmilesError:
        return canonical_text(label)


def _identity_labels(labels: list[str]) -> dict[str, str]:
    """Every label of one categorical, mapped to the string the identity payload uses for it.

    The reduction is per label, but the *decision* is per space, because a reduction mapping two of
    one space's own labels onto one string is not a canonicalisation of that space — it is a
    smaller space. `structures` and `descriptors` are maps keyed by these labels, so a colliding
    reduction does not merely lose a distinction: the dict comprehension building the payload
    **drops an entry**. Measured, `{"L1": "CCO", "l1": "CCN"}` and `{"L1": "c1ccccc1", "l1": "CCN"}`
    hashed to one campaign, one feature space silently standing in for the other.

    Only the colliding labels keep their exact spelling, never the whole space: reverting a space
    wholesale would put the caller's casing back into the identity of every *other* label in it,
    which is the fork the fold exists to close — `["THF", "thf", "Toluene"]` must still reach the
    same id as `["thf", "THF", "toluene"]`.

    A raise would be the louder answer and is the wrong one here. `record_suggestion` derives the id
    outside its own failure handling, so raising would turn a computed suggestion into a failed tool
    call over a space that is perfectly legal — `CategoricalParameter` asks only that the labels be
    distinct, and these are.
    """
    reduced = {label: _identity_label(label) for label in labels}
    collisions = {value for value, count in Counter(reduced.values()).items() if count > 1}
    return {label: label if value in collisions else value for label, value in reduced.items()}


def _space_of(parameter: Parameter) -> dict[str, Any]:
    """One parameter as the identity sees it, canonicalised.

    **Names and labels are reduced**, because the caller is a model re-emitting a decision space it
    read back out of `resume_campaign` — the loop this tool documents, "resume, append the chemist's
    new result, then ask again". Measured over the ways a model perturbs a value it is copying:
    reordering parameters and reordering categories were already handled and re-emitting `20` for
    `20.0` is harmless, but **re-casing a category, a parameter name or an objective name, or a
    trailing space on any of them, minted a different campaign** — a fresh row with no history,
    silently, because `record_suggestion` upserts and nothing compares the two. That is strictly
    worse than the duplicate run the identical defect caused in `_report_id`, which folded for
    exactly this reason.

    A name is free text and folds (`core.ids.canonical_text`); a *category label* may be a molecule
    and is reduced by `_identity_label` instead, which is the same act on a data type where case
    carries meaning. `_identity_labels` then decides the space as a whole, so a reduction that would
    merge two of its own labels is not applied to them.

    Bounds are rounded to `_BOUND_DECIMALS` on the same argument: a model re-emitting `120.0` as
    `120.00000001` is not widening a search space, and the difference is far below any bound a
    chemist states.

    **Descriptors identify the space unless they were computed from the structures.** When
    `structures` is set, `featurize_problem` derives the descriptors from it, so a cache miss
    recomputing the same values — or a calculator upgrade shifting the sixth decimal — must not fork
    the campaign, and the structures already identify the chemistry. When `structures` is `None` the
    caller supplied the descriptors directly and they are the **only** statement of what the
    surrogate sees: excluding them collapsed a bare categorical, one featurized on `{A: 1, B: 2}`
    and one on `{A: 99, B: -99}` onto a single id, three genuinely different feature spaces sharing
    one row that `record_suggestion` then overwrites — the "seeded with observations from a
    different campaign" failure `read_campaign_thread` exists to prevent.
    """
    dumped = parameter.model_dump(mode="json", include=_SPACE_FIELDS)
    dumped["name"] = canonical_text(str(dumped["name"]))
    for bound in ("lower", "upper"):
        if dumped.get(bound) is not None:
            dumped[bound] = round(float(dumped[bound]), _BOUND_DECIMALS)
    if isinstance(parameter, CategoricalParameter):
        labels = _identity_labels(parameter.categories)
        # The set of choices is the space; the order they were typed in is not. `["THF","toluene"]`
        # and `["toluene","THF"]` offer the same experiments and hashed to two campaigns. Sorted
        # here in the *identity* payload only — the problem the surrogate sees keeps the caller's
        # order, because a bare `CategoricalInput` is ordinally encoded and reordering it moves the
        # acquisition optimizer (measured: `equiv` 2.1018 vs 2.0691 on one fixed-seed round). That
        # jitter is far below experimental resolution; a split history is not.
        dumped["categories"] = sorted(labels.values())
        # `structures` and `descriptors` are maps *keyed by* those labels, so both are re-keyed
        # through the one map rather than reduced a second time: a key the identity no longer
        # contains addresses nothing, and two reductions of one label set can only ever disagree.
        # The SMILES themselves are never touched — they are values, and a value that is a
        # structure is the case `_identity_label` exists for.
        if parameter.structures is not None:
            dumped["structures"] = {
                labels[label]: smiles for label, smiles in parameter.structures.items()
            }
        elif parameter.descriptors is not None:
            # Added only when it carries information, by the rule the objectives and constraints
            # keys already follow: a bare categorical must hash to the payload it hashed to before
            # this existed, or every recorded campaign over one becomes unreachable.
            dumped["descriptors"] = {
                labels[label]: row
                for label, row in parameter.model_dump(mode="json", include={"descriptors"})[
                    "descriptors"
                ].items()
            }
    return dumped


def _canonical(constraint: Constraint) -> dict[str, Any]:
    """One constraint as the identity sees it, in a form the caller's ordering cannot change.

    `base + acid <= 3` and `acid + base <= 3` are the same polytope and must be the same campaign.
    Hashing the dump directly made them two, each with an empty history — the same silent fork the
    allowlist above exists to prevent, on the field that comment did not cover.
    """
    dumped = constraint.model_dump(mode="json")
    if isinstance(constraint, ExcludeConstraint):
        # The options are category labels, so they are reduced by the rule the labels are
        # (`_identity_labels`) — an exclusion naming piperidine and one naming pyridine are
        # different campaigns, and over a library holding both the space alone cannot say so.
        dumped["pairs"] = sorted(
            [canonical_text(name), sorted(_identity_labels(options).values())]
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

    The direction is a closed set (`minimize`/`maximize`) and is not free text, so it is left
    alone — folding a value pydantic already constrains buys nothing and hides where the rule
    applies.
    """
    dumped = objective.model_dump(mode="json")
    dumped["name"] = canonical_text(str(dumped["name"]))
    return dumped


def campaign_id_for(problem: OptimizationProblem) -> str:
    """The stable id of the campaign this problem *is*.

    Derived from the decision space and the objective rather than minted per call, which is the
    whole difference between a campaign and a turn: asking twice about one optimization must reach
    one campaign, or the history a campaign exists to accumulate never accumulates.

    **Descriptors are excluded when they were computed from the structures, and included when the
    caller stated them** — see `_space_of`. Constraints are canonicalized so the order the caller
    happened to write a sum in cannot fork a campaign — see `_canonical`.

    **Every model-authored name is reduced and every bound rounded** (`_space_of`,
    `_objective_identity`), because the caller re-types this whole structure from what
    `resume_campaign` handed back and a model re-cases freely. `docs/decisions/` records the
    measurement; the short form is that `THF` and `thf` were two campaigns with two empty histories
    — while `C1CCNCC1` and `c1ccncc1`, piperidine and pyridine, were one campaign with one, which
    is why a category label is reduced as chemistry rather than as text (`_identity_label`).
    **Existing rows are re-keyed rather than orphaned** — `chemclaw.cli.rekey_campaigns` recomputes
    the id from each row's stored `problem`, which is why this could change at all.

    **The parameter list and each parameter's categories are canonicalized for the same reason**,
    which the constraint fix did not extend to them: a space is a set of axes, and the order a
    caller listed them in carries no information — measured, `[T, E, S]` and `[S, E, T]` propose
    byte-identical candidates under a fixed seed — yet each ordering minted its own campaign with
    its own empty history. `objectives` is deliberately *not* sorted: the lead objective is
    privileged (`Campaign.objective`, `_objective_output`), so their order is semantic.
    """
    space = sorted(
        (_space_of(parameter) for parameter in problem.parameters),
        key=lambda dumped: str(dumped["name"]),
    )
    # The legacy key, always, spelled exactly as it was. A single-objective problem must hash to the
    # byte-identical payload it hashed to before `objectives` existed, or every recorded campaign
    # becomes unreachable — `read_campaign_thread` would tell each chemist their campaign is new.
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
        identity["constraints"] = sorted(
            (_canonical(constraint) for constraint in problem.constraints),
            key=lambda dumped: json.dumps(dumped, sort_keys=True),
        )
    return f"campaign-{stable_hash(identity)}"


class Campaign(BaseModel):
    """One optimization problem, tracked across the turns that refine it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    campaign_id: str = Field(min_length=1)
    # The **lead** objective only, for display and for querying by hand. Since multi-objective
    # shipped, a trade-off campaign's second and further axes live in `problem["objectives"]`, which
    # is authoritative; this pair cannot represent them, so a report reading only these two sees a
    # trade-off as single-objective. Identity is unaffected — `campaign_id_for` hashes the whole
    # list. No column was added because nothing queries by objective yet; when something does, read
    # `problem->'objectives'` or add a generated column off it.
    #
    # This note lived in `infra/sql/031_bo_campaigns.sql` for a few hours and had to move: editing
    # an applied migration changes its checksum, and the runner then refuses to migrate *any*
    # database that already had it — a comment took every existing deployment's migrations down
    # while CI, which always starts from an empty database, stayed green
    # (D-2026-08-04-the-schema-only-goes-forward). It belongs with the code that writes the columns
    # anyway.
    objective: str = Field(min_length=1)
    # `str`, not `Objective`'s `Literal["minimize", "maximize"]`, and deliberately so: this model is
    # built from a `bo_campaigns` row on every `resume_campaign`, so a narrower type would make a
    # row carrying an unexpected value permanently unreadable rather than odd — the same hazard
    # that keeps `require_names_do_not_clash` out of `OptimizationProblem`'s validators. What is
    # *written* is always one of the two, because it comes from `problem.objective.direction`.
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
    # The decision space **as it was when this was proposed**. `Campaign.problem` holds the latest
    # one — a chemist who widens a bound is still working the same optimization — so a suggestion
    # read back after the space changed was described by bounds that never applied to it. The
    # candidates and observations are snapshotted for exactly this reason; the space they were
    # drawn from is the third piece of the same statement (`infra/sql/037_*.sql`).
    problem: dict[str, Any] = Field(default_factory=dict)
    # The durable run that produced this, empty for the inline tool. The idempotency key: an
    # activity is retried by design, and without it a retry appends a second identical suggestion
    # into a history that is meant to be a record of what was actually proposed.
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

        Returns the suggestion id and whether this call created the campaign, in that order.

        One method rather than two, because the two writes are not independent: the upsert replaces
        the stored `problem`, so a failure between them joins the new decision space to the old
        evidence. The created flag is returned by the write rather than read before it, because a
        read-then-write cannot answer "did I open this campaign" without racing another turn that
        is opening the same one.
        """
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

    async def record(self, campaign: Campaign, suggestion: Suggestion) -> tuple[int, bool]:
        """Both writes or neither, as the Postgres sibling does.

        Atomic here for free — nothing between the two statements can fail — but written as one
        method so the two backends cannot drift into different contracts. The created flag comes
        out of the upsert for the same reason it does there: it is a property of what the write
        did, not of what a prior read saw.
        """
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

        The same rule `bo_suggestions_job_idx` enforces in Postgres, written here rather than left
        to the other backend: a `session_store="memory"` deployment runs the same durable campaigns
        through the same retried activity, so an idempotency guarantee that held only against
        Postgres would be a guarantee nothing in a dev stack could rely on. Keyed on the run, never
        on the content — two genuinely identical asks are two history entries.
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

    Cached so the writer and any reader share one instance, which is what makes the in-memory
    backend accumulate rather than start empty on every call — the same reason
    `plan_approval_store` and `proposal_store` are cached.
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

    **Never raises on a database failure**, and always raises on ours. The candidates are already
    computed and are what the chemist asked for, so a blip must not turn a successful suggestion
    into a failed tool call — the trade
    `chemclaw.agent.audit` and `chemclaw.kg.proposal` both make, for the same reason: the record is
    about the thing, and losing it must not cost the thing.

    Returns the campaign id either way, because the id is a pure function of the problem: it is
    still the right handle for the agent to quote back, even on the turn where the write failed.
    `opened_new_campaign` is `False` on that turn — not because the campaign is known, but because
    a failed write is ignorance, and announcing a fork on the strength of one would send a chemist
    looking for a problem a database outage invented. Same direction the read it replaced erred in.

    Args:
        problem: The decision space, which both identifies the campaign and is snapshotted onto the
            suggestion — the campaign row holds the *latest* space, the suggestion the one it was
            made in.
        candidates: The proposed point(s).
        observations: The evidence they were derived from.
        calc_refs: The calculation keys the descriptors came from.
        provenance: `(actor, session_id, correlation_id)`, as `connectors.caller` yields it.
        job_id: The durable run that produced this, empty for the inline tool. Makes the write
            idempotent — a Temporal activity is retried by design, and the durable campaign is the
            second writer this record has ever had.
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
        # Narrow, and at WARNING because the chemist still has their candidates: a database blip
        # must not turn a computed suggestion into an error. Everything else — a `ValidationError`
        # from the models above, a `TypeError`, a non-finite float refused by the store — is a
        # defect in this code, and swallowing it made a deployment where 100% of BO writes fail
        # indistinguishable from one where none do.
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
        last_asked_at=campaign.last_asked_at,
    )
