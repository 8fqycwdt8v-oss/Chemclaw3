"""The `bo` connector's MCP tool surface: one-shot Bayesian-optimization experiment design.

Exposes BoFire's ask step so "which experiment should I run next?" is answered from data the agent
has gathered: it assembles the decision space and the runs so far and asks for the next point(s).
A single ask is inline (the GP fit runs off the event loop); the durable `BoCampaignWorkflow` is
the path for an automated closed loop. `resume_campaign` reads back the suggestions recorded per
campaign, so the ask→observe→ask loop can cross sessions.

Deterministic capability only; the judgment lives in the bundled `experiment-design` skill.
`suggest_next_experiment` and `predict_outcome` featurize (xTB, writing `calculation_results`) and
the first also writes `bo_campaigns`/`bo_suggestions`, so both are `state_changing` in
`connector.yaml`; `tests/test_bo_tools.py` derives that from these bodies. Only the neutral
`chemclaw.science.bo.problem` types cross the connector boundary, keeping `bofire` and `botorch`
out of the chat service's image.
"""

import asyncio
import json
import statistics
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field, computed_field

from chemclaw.connectors.bo.calculators import properties_for
from chemclaw.connectors.caller import caller_provenance
from chemclaw.core.ids import stable_hash
from chemclaw.science.bo.campaign_record import (
    CampaignThread,
    read_campaign_thread,
    record_suggestion,
)
from chemclaw.science.bo.engine import (
    factorial_design,
    initial_candidates,
    interrogate_surrogate,
    optimal_design,
    propose_candidates,
)
from chemclaw.science.bo.featurize import featurize_problem
from chemclaw.science.bo.problem import (
    Candidate,
    ContinuousParameter,
    FitQuality,
    Objective,
    Observation,
    OptimalDesign,
    OptimizationProblem,
    ParamValue,
    Prediction,
    ScreeningDesign,
    observed_value,
    pareto_front,
    require_descriptors_distinguish_categories,
    require_names_do_not_clash,
    require_observations_cover_objectives,
)
from chemclaw.science.bo.progress import CampaignProgress
from chemclaw.science.bo.progress import campaign_progress as read_progress
from chemclaw.science.calc.postgres_store import default_store

server = FastMCP("bo")


class ObjectiveScale(BaseModel):
    """What the objective's numbers actually span in the runs supplied, so an sd can be read.

    `Candidate.predicted_sd` comes back in the objective's own units with nothing beside it, and a
    number with no scale is not an explanation: +/-3 is an exploit of chemistry the model has
    learned when the observed yields span 40 points, and an excursion into chemistry it has not
    when they span 4. The audit called story 3.4 "a rubric gap, not a computation gap" — the sd was
    already computed and returned; what was missing was the thing to compare it to.
    """

    name: str = Field(min_length=1)
    direction: str = Field(min_length=1)
    n: int = Field(ge=0)
    observed_min: float | None = None
    observed_max: float | None = None
    # Sample standard deviation of the observed values; None below two runs, where it is undefined
    # rather than zero.
    observed_sd: float | None = Field(default=None, ge=0.0)

    @property
    def spread(self) -> float | None:
        """Observed max minus min, or None when there is nothing to span."""
        if self.observed_min is None or self.observed_max is None:
            return None
        return self.observed_max - self.observed_min


def _objective_scale(
    problem: OptimizationProblem, history: list[Observation], objective: Objective
) -> ObjectiveScale:
    """Summarize what one objective did across the runs supplied."""
    values = [observed_value(problem, observation, objective.name) for observation in history]
    return ObjectiveScale(
        name=objective.name,
        direction=objective.direction,
        n=len(values),
        observed_min=min(values) if values else None,
        observed_max=max(values) if values else None,
        observed_sd=statistics.stdev(values) if len(values) > 1 else None,
    )


class ExperimentSuggestion(BaseModel):
    """Proposed experiments, the campaign they belong to, and the calculations behind the space.

    A richer return than the bare `list[Candidate]` it replaces, because the candidates alone were
    the part that was never the problem: they were computed, returned, and — with nothing else
    carried out of the call — the framing that produced them was discarded every turn. The
    `campaign_id` is what a later turn quotes to add observations to the same optimization, and
    `calc_refs` is what an `experiment-proposal` note cites so a stale calculation can be traced to
    the experiment it suggested.
    """

    campaign_id: str = Field(min_length=1)
    candidates: list[Candidate] = Field(default_factory=list)
    # How many were asked for, so a short batch says so. `propose_candidates` may return fewer than
    # `n`
    # when a finite space runs low (the durable loop stops on `space_exhausted` first, so this
    # affects
    # only inline answers). Zero means "not stated" and suppresses the clause.
    requested: int = Field(default=0, ge=0)
    calc_refs: list[str] = Field(default_factory=list)
    # What the objective spans in the runs behind these candidates, so each candidate's
    # `predicted_sd` can be read against something. One per objective, lead first.
    scale: ObjectiveScale | None = None
    scales: list[ObjectiveScale] = Field(default_factory=list)
    # The non-dominated subset of the observations the caller supplied — the trade-off the runs
    # show.
    # Empty on a single-objective problem.
    front: list[Observation] = Field(default_factory=list)
    # The assay reproducibility the front was drawn with, or None for exact precision, so the
    # summary
    # can say which.
    front_tolerance: float | None = None
    # True when this ask opened a campaign with no prior suggestions while observations were
    # supplied —
    # almost always an accidental fork. A campaign id hashes its decision space, so an added option
    # or a
    # moved bound is a new campaign; `campaign_id_for` canonicalises away re-typing, and this
    # reports the
    # rest, which `record_suggestion`'s upsert would otherwise make look like a first ask.
    opened_new_campaign: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """Each candidate's posterior sd read against the observed spread, in one sentence each.

        A `computed_field` so it is serialized and reaches the model composing the answer.
        """
        if not self.candidates:
            return "No candidates were proposed."
        spread = self.scale.spread if self.scale else None
        readings = []
        if self.requested > len(self.candidates):
            # First, because a short batch otherwise reads as a complete answer. The cause is always
            # a finite,
            # all-categorical space with fewer fresh conditions than requested.
            readings.append(
                f"You asked for {self.requested} candidate(s) and only {len(self.candidates)} "
                "could be proposed: every other condition in this decision space has already been "
                "run. This is a nearly-complete screen, not a shortlist — say so, and do not "
                "present these as the best of a wider search."
            )
        if len(self.scales) > 1:
            named = ", ".join(f"{scale.direction} {scale.name}" for scale in self.scales)
            # `is None`, not truthiness: an explicit `assay_noise=0.0` means "compare exactly",
            # which is a stated choice, and reporting it as "none given" would be false.
            drawn = (
                "No assay reproducibility was given, so every numeric difference counted as "
                "real — pass `assay_noise` and the front will usually be longer and truer."
                if self.front_tolerance is None
                else f"Runs differing by {self.front_tolerance:.3g} or less were treated as "
                "indistinguishable, so neither knocked the other off."
            )
            if not self.scales[0].n:
                # Cold start: the trade-off sentence below would describe an empty front, which the
                # model would
                # resolve by inventing one.
                readings.append(
                    f"This is a trade-off over {len(self.scales)} objectives ({named}), and no "
                    "runs were supplied, so there is no front yet: `front` is empty because "
                    "nothing has been measured, not because nothing survived. The points below are "
                    "space-filling seeds covering the space; measure every objective on each and "
                    "pass them back to get the first trade-off."
                )
            else:
                readings.append(
                    f"This is a trade-off over {len(self.scales)} objectives ({named}), so there "
                    f"is no single best point. `front` holds the {len(self.front)} run(s) of those "
                    "supplied that nothing else beats on every objective at once — quote those as "
                    f"the trade-off, and read the sd below against the lead objective only. {drawn}"
                )
        for index, candidate in enumerate(self.candidates, start=1):
            if candidate.predicted_sd is None:
                readings.append(
                    f"Candidate {index} is a space-filling seed point — no surrogate had an "
                    "opinion about it. That is not an endorsement."
                )
                continue
            if spread is None or spread <= 0:
                readings.append(
                    f"Candidate {index}: posterior sd +/-{candidate.predicted_sd:.3g}, with too "
                    "little spread in the runs supplied to say whether that is large or small."
                )
                continue
            share = candidate.predicted_sd / spread
            reading = "an exploit of a region the model has learned"
            if share >= 0.5:
                reading = "an excursion into chemistry the model has not seen"
            elif share >= 0.2:
                reading = "a step beyond the runs supplied, but not a leap"
            readings.append(
                f"Candidate {index}: posterior sd +/-{candidate.predicted_sd:.3g} against an "
                f"observed spread of {spread:.3g} ({share:.0%}) — {reading}."
            )
        return " ".join(readings)


def _require_observed_params_match(
    problem: OptimizationProblem, history: list[Observation]
) -> None:
    """Reject an observation whose parameters are not exactly the problem's declared ones.

    BoFire indexes experiments by the domain's input keys, so a mismatch surfaces deep in the
    library,
    and `chemclaw.connectors.server` forwards only `ValueError` verbatim. A missing declared
    parameter
    already fails in BoFire's `validate_experimental`; this adds a clearer message. An undeclared
    extra parameter would silently succeed with the stray column ignored — this turns that wrong
    answer into a fixable error. (A specific live `KeyError: 'base'` that prompted this was never
    reproduced; this closes the class, not provably that instance.)

    Raises:
        ValueError: Naming the offending observation's index and the parameter(s) at fault.
    """
    _require_params_match(problem, [observation.params for observation in history], "observations")


def _require_points_match(
    problem: OptimizationProblem, points: list[dict[str, ParamValue]]
) -> None:
    """The same check for the points `predict_outcome` is asked about, plus a value check.

    Names are checked as for observations. Values too, because `strategy.predict` validates nothing:
    an undeclared level or a non-numeric continuous value would raise a `KeyError`/`TypeError` that
    the server sanitises to "an internal error". A value outside a continuous bound is deliberately
    allowed — it is answered as an extrapolation, labelled by `Prediction.in_domain`; this asks only
    whether the surrogate can encode the value at all.

    Raises:
        ValueError: Naming the offending point's index and the parameter(s) at fault.
    """
    _require_params_match(problem, points, "points")
    for index, params in enumerate(points):
        for parameter in problem.parameters:
            # `_require_params_match` has already established that every declared name is present.
            value = params[parameter.name]
            if isinstance(parameter, ContinuousParameter):
                # `bool` is an `int` in Python, and `True` would reach the surrogate as 1.0 — a
                # temperature of 1 °C answered confidently for a caller who meant something else.
                if isinstance(value, bool) or not isinstance(value, int | float):
                    raise ValueError(
                        f"points[{index}] gives {value!r} for {parameter.name!r}, which is a "
                        f"continuous parameter and needs a number. Its declared range is "
                        f"{parameter.lower:g} to {parameter.upper:g}; a value outside that range "
                        "is allowed and will be answered as an extrapolation."
                    )
            elif value not in parameter.categories:
                raise ValueError(
                    f"points[{index}] gives {value!r} for {parameter.name!r}, which is not one of "
                    f"its options {parameter.categories}. Unlike a continuous range, a category "
                    "has no outside for the model to extrapolate into: it never saw this level and "
                    "has no way to represent it. Ask about one of the options listed, or add this "
                    "one to the problem's categories (with its `structures` entry if the others "
                    "have one) — which makes it a different decision space."
                )


def _require_params_match(
    problem: OptimizationProblem, assignments: list[dict[str, ParamValue]], noun: str
) -> None:
    """The one definition of "these parameters are the problem's parameters"."""
    declared = {parameter.name for parameter in problem.parameters}
    for index, params in enumerate(assignments):
        given = set(params)
        missing = sorted(declared - given)
        undeclared = sorted(given - declared)
        if not missing and not undeclared:
            continue
        faults = []
        if missing:
            faults.append(f"no value for the declared parameter(s) {missing}")
        if undeclared:
            faults.append(f"a value for {undeclared}, which the problem does not declare")
        raise ValueError(
            f"{noun}[{index}] has {' and '.join(faults)} — every entry must give a "
            "value for exactly the parameters the problem declares. Add the missing value(s), "
            "drop the extra one(s), or change the problem's parameters to match the runs you have."
        )


# The namespace stamped onto an actor this process cannot authenticate. A constant, not a setting:
# it is part of the record's shape across deployments.
_UNVERIFIED_ACTOR_PREFIX = "unverified:"


def _recorded_provenance() -> tuple[str, str, str]:
    """The caller's `(actor, session_id, correlation_id)`, with the actor marked unauthenticated.

    `caller_provenance` reads the actor from the unauthenticated `X-Chemclaw-Actor` header, and the
    values land in `bo_campaigns.opened_by` and `bo_suggestions.actor`. The bundle's bearer proves
    the
    caller holds its credential (core, in the shipped deployment), not which chemist core was
    serving, so the actor is recorded as `unverified:<name>`. A synchronous MCP call has no channel
    for
    a validated principal (the durable path reads `requested_by` off the Temporal memo instead).

    An absent actor stays empty. `session_id` and `correlation_id` pass through unmarked: they are
    join keys that let an auditor recover the validated actor from core's audit trail.
    """
    actor, session_id, correlation_id = caller_provenance()
    return (
        f"{_UNVERIFIED_ACTOR_PREFIX}{actor}" if actor else actor,
        session_id,
        correlation_id,
    )


def _as_list(value: object, noun: str) -> list[Any]:
    """Accept an array the model JSON-*encoded* as a string, and refuse anything that is not a list.

    Models occasionally emit a large observations array as one JSON string, which schema validation
    would reject before the body runs. Decoding is more permissive, but `json.loads` accepts any
    JSON,
    so a non-list result is refused rather than iterated.

    Raises:
        ValueError: When the string does not decode, or decodes to something that is not a list.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError(
                f"{noun} arrived as a string that is not valid JSON ({error}). Send it as a JSON "
                "array of objects, not as text."
            ) from error
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(
            f"{noun} must be an array of objects; got {type(value).__name__}. Send one entry per "
            "run, each an object, or an empty array if there are none yet."
        )
    return value


@server.tool()
async def suggest_next_experiment(
    problem: OptimizationProblem,
    observations: list[Observation] | str | None = None,
    count: int = 1,
    assay_noise: float | None = None,
) -> ExperimentSuggestion:
    """Suggest the next experiment(s) to run for an optimization problem (Bayesian optimization).

    Answers "what should I try next?" Give the decision space (which conditions may vary and
    their ranges/choices) and the runs done so far (their conditions and the measured
    objective); it returns the point(s) a surrogate model expects to be most informative. With
    no observations yet it returns space-filling seed points instead (a model needs data first).
    These are *proposals a human runs* — surface them, do not treat them as results.

    **Several objectives are supported: give them all.** `problem.objectives` is a list, so yield
    *and* selectivity, or conversion *and* impurity, go in together with a direction each. The
    optimizer then searches the trade-off rather than one axis, and the return's `front` holds the
    runs among those you supplied that nothing else beats on every objective at once. Present that
    front as the trade-off and let the chemist choose along it — do **not** announce a single
    "best" point for a multi-objective problem, because there is not one. Every observation must
    then report every objective, in its `values` map.

    **A limit across several parameters is a constraint; give it as one.** "Base plus acid under 3
    equivalents", "water at most 5% of the solvent", "these fractions sum to 1" go in
    `problem.constraints` as `{parameters, coefficients, relation, rhs}`, and the optimizer honours
    them — every candidate it returns satisfies them, including the space-filling seed points.

    A limit on **one** parameter is not a constraint: "keep the temperature under 80 °C" is that
    parameter's upper bound, and writing it as a constraint instead is a worse way to say the same
    thing. That linear form applies to continuous parameters only. Note that a constraint makes the
    search **several times slower** — measured, about three times the unconstrained cost for one
    candidate and roughly nine seconds per further candidate — so ask for a small batch on a
    constrained problem.

    **A forbidden pairing of options is the other constraint shape.** "Never Pd(OAc)₂ in DMSO" is
    `{"kind": "exclude", "parameters": [...], "options": [[...], [...]]}` — for options that are
    each fine alone and only bad together. A forbidden option on its own is simply one you leave out
    of the category list. An exclusion needs an **all-categorical** problem: BoFire applies it by
    enumerating the space, so one continuous parameter anywhere makes it unusable and the tool will
    say so.

    **Continuing an earlier campaign?** Call `resume_campaign(campaign_id)` first to recover the
    decision space and the runs it already has, then add the new results and call this tool.

    Build `problem` and `observations` from evidence you have gathered (e.g. past runs of the
    transformation via similar_reactions / an optimization-campaign note), so the suggestion
    rests on real history. Mark each observation's `provenance` "measured" for lab data or
    "predicted" if it came from a model, keeping the campaign honest.

    **When a categorical choice is a molecule** — a ligand, base, solvent, or catalyst — give
    its `structures` (a mapping from each category label to its SMILES). Each option is then
    described by computed electronic descriptors instead of being an opaque label, so the
    model can reason about an option nobody has run yet rather than only about the ones with
    data. This costs one fast calculation per option and is cached thereafter.

    Args:
        problem: The decision variables (continuous/categorical), the objective(s) — each a name
            plus minimize/maximize — and any `constraints`: a linear limit coupling two or more
            continuous parameters, or an exclusion forbidding a pairing of categorical options.
            Set a categorical's `structures` when its options are molecules.
        observations: Runs already done, each mapping the parameter values to the objective
            value. Every observation must give a value for *every* parameter `problem` declares
            and name no others — a run whose conditions you only partly know cannot seed this. For
            several objectives, give each run's `values` map naming every one of them.
            Omit or pass an empty list to get seed points for a fresh campaign.
        count: How many candidates to propose (a batch).
        assay_noise: The assay's reproducibility in the lead objective's own units, if the chemist
            has stated one ("+/-2%" is `2.0`). Used only for the `front` on a multi-objective
            problem: two runs differing by less than this are not distinguishable, so neither
            beats the other and both stay on the front. Without it the front treats every numeric
            difference as real and is usually shorter than the chemist's true trade-off. Same
            number and same meaning as `campaign_progress`'s required argument.

    **If `opened_new_campaign` comes back true, say so before presenting the candidates.** It
    means runs were supplied against a decision space this system has never been asked about, which
    is usually a space that drifted from the one the chemist has been working — an option added, a
    bound moved — and the history is now split across two campaigns. Ask which they meant rather
    than reporting the suggestion as a continuation.

    The suggestion is **recorded** against the campaign this problem defines, and the returned
    `campaign_id` is the handle for it. Quote that id back to the chemist: asking again about the
    same decision space accumulates onto the same campaign, so the sequence of proposals — and the
    evidence each rested on — becomes the campaign's history instead of being discarded with the
    turn. `calc_refs` names the calculations behind the decision space's descriptors; pass them to
    `record_knowledge_note` if you draft an `experiment-proposal` note from this, so a stale
    calculation can be traced to the experiment it suggested.

    Returns:
        The proposed candidate point(s), the campaign they belong to, and the calculation keys the
        decision space was built from.
    """
    # A tool call arrives as JSON, so nested models arrive as plain dicts and are re-validated here;
    # `model_validate` is a no-op for callers already passing real models.
    problem = OptimizationProblem.model_validate(problem)
    history = [Observation.model_validate(item) for item in _as_list(observations, "observations")]
    # The tool owns its contract, so the declared/observed parameter agreement is checked here —
    # after the models exist and before anything downstream indexes by parameter name.
    require_names_do_not_clash(problem)
    _require_observed_params_match(problem, history)
    require_observations_cover_objectives(problem, history)
    # Featurize before the engine sees the problem, including on the seeding path, so declared
    # structures are modelled by descriptors rather than as opaque categories. Needs the coerced
    # problem.
    featurized = await featurize_problem(properties_for(default_store()), problem)
    # After featurization, not before: two labels pointing at the same molecule are distinct until
    # xTB gives them the same descriptor row, and from there the surrogate cannot tell them apart.
    require_descriptors_distinguish_categories(featurized.problem)
    if history:
        candidates = await asyncio.to_thread(propose_candidates, featurized.problem, history, count)
    else:
        candidates = await asyncio.to_thread(initial_candidates, featurized.problem, count)
    # Recorded after the candidates exist; `record_suggestion` swallows its own failures so a
    # database
    # blip never costs the suggestion. The campaign id is a pure function of the problem, so it is
    # returned even when the write did not land. `_recorded_provenance` marks the header-supplied
    # actor
    # as unverified. The fork signal comes from the write itself, so concurrent openers cannot both
    # claim it (`RecordedSuggestion`).
    #
    # `job_id` makes the write idempotent for replays of a killed call: a hash over the campaign,
    # the
    # request and the history, so identical requests over identical evidence dedupe and a new
    # observation changes the key.
    inline_key = "inline-" + stable_hash(
        [
            featurized.problem.model_dump(mode="json"),
            [candidate.model_dump(mode="json") for candidate in candidates],
            [observation.model_dump(mode="json") for observation in history],
        ]
    )
    recorded = await record_suggestion(
        problem=featurized.problem,
        candidates=candidates,
        observations=history,
        calc_refs=featurized.calc_refs,
        provenance=_recorded_provenance(),
        job_id=inline_key,
    )
    campaign_id = recorded.campaign_id
    scales = [_objective_scale(problem, history, obj) for obj in problem.objectives]
    return ExperimentSuggestion(
        campaign_id=campaign_id,
        candidates=candidates,
        requested=count,
        calc_refs=featurized.calc_refs,
        scale=scales[0],
        scales=scales,
        # The front of the caller's runs, not of predictions: a trade-off is a statement about
        # measurements. Empty for one objective.
        front=(
            pareto_front(problem, history, assay_noise or 0.0)
            if len(problem.objectives) > 1
            else []
        ),
        front_tolerance=assay_noise,
        opened_new_campaign=bool(history) and recorded.opened_new_campaign,
    )


@server.tool()
async def campaign_progress(
    problem: OptimizationProblem,
    observations: list[Observation] | str,
    assay_noise: float,
    window: int | None = None,
    objective: str | None = None,
) -> CampaignProgress:
    """Has this optimization plateaued, or is there more in it? Read against the assay's own noise.

    Use this whenever a chemist asks whether to keep going — "have we plateaued", "is there more in
    it", "I don't want to burn another two weeks" — instead of reflexively proposing another
    candidate. It answers from the runs themselves: the best so far, how long since a gain larger
    than the noise, and whether the most recent results differ from each other at all.

    **`assay_noise` is required and you must get it from the chemist**, in the objective's own
    units — "assay reproducibility is about +/-2%" means `assay_noise=2.0` for a yield objective. If
    they have not said, ask before calling; do not invent one and do not call this without it. A
    gain of 1-2% against a +/-2% assay is not a gain, and saying it is has already been graded a
    fabrication once.

    **What this does and does not establish.** It reads the runs you supply and nothing else, so it
    can never show a global optimum has been reached — only that recent points *in the region
    already explored* have not beaten the noise. The returned `summary` says so; repeat that limit
    rather than rounding it up to "converged".

    **To speak to what the model expects next**, call `suggest_next_experiment` as well and compare
    its candidates' `predicted_value` against the same `assay_noise`: a predicted improvement inside
    the noise is not a reason to run another experiment, and a large `predicted_sd` somewhere
    untried is. This tool deliberately asks no surrogate — "the record stopped moving" and "the
    model expects nothing" are different claims with different evidence.

    Args:
        problem: The decision space and objective, in the same shape `suggest_next_experiment`
            takes — pass the one `resume_campaign` returned, or the one you just built.
        observations: The runs so far, **in the order they were performed**. Order matters: a list
            sorted by value would report a campaign that never stopped improving.
        assay_noise: The assay's reproducibility in the objective's own units. Required.
        window: How many recent evaluations the "do these differ at all" statement covers. Defaults
            to the configured window; set it explicitly when the chemist points at a specific tail
            ("the last four runs").
        objective: Which objective to read, when the problem has several. A trade-off plateaus per
            axis — yield can stop moving while the impurity is still falling — so this is required
            there and the call is refused without it.

    Returns:
        The best so far, the running best per evaluation, how many evaluations since a real gain,
        the spread of the recent window, a plateau verdict, and a `summary` stating the limit.
    """
    problem = OptimizationProblem.model_validate(problem)
    history = [Observation.model_validate(item) for item in _as_list(observations, "observations")]
    require_names_do_not_clash(problem)
    _require_observed_params_match(problem, history)
    require_observations_cover_objectives(problem, history)
    # Off the event loop: `discrete_candidate_count` can walk the whole categorical cross product
    # (bounded by `bo_max_enumerated_cells`), and both inputs come from the model.
    return await asyncio.to_thread(read_progress, problem, history, assay_noise, window, objective)


@server.tool()
async def resume_campaign(campaign_id: str) -> CampaignThread:
    """Pick up a Bayesian-optimization campaign started in an earlier turn or session.

    Use this whenever a chemist refers back to an optimization already under way — "here is the
    result of the run you suggested", "where did we get to on the amination screen" — and you have
    the `campaign_id` from a previous `suggest_next_experiment` answer. It returns the decision
    space as it was framed, the runs the last suggestion rested on, and the candidates that
    suggestion proposed, so the next ask builds on the campaign's real history instead of on
    whatever survived in the conversation.

    The normal follow-up is: resume, append the chemist's new result to `observations`, then call
    `suggest_next_experiment` with the returned `problem` and the extended observation list.

    A campaign id is a **hash of the decision space**, not a serial number. So an id that does not
    resolve almost always means the space has changed since — a widened bound, an added or swapped
    option — and the new space is a different campaign with no history yet. This tool raises in
    that case rather than quietly answering from nothing; treat the error as "this is a new
    campaign", say so, and ask for a fresh suggestion.

    Args:
        campaign_id: The id quoted in an earlier `suggest_next_experiment` answer.

    Returns:
        The campaign's objective and direction, its decision space, the observations behind its
        latest suggestion, and the candidates that suggestion proposed.
    """
    return await read_campaign_thread(campaign_id)


@server.tool()
async def generate_screening_design(
    problem: OptimizationProblem,
    n_generators: int = 0,
    n_center: int = 0,
    n_repetitions: int = 1,
    randomize: bool = False,
    criterion: str = "factorial",
    n_experiments: int = 0,
    formula: str = "linear",
) -> ScreeningDesign | OptimalDesign:
    """Generate a factorial screening design — full grid or reduced, categorical or continuous.

    Use this for the *other* classical DoE question — "run every combination of these conditions" —
    e.g. every catalyst x solvent x base combination before narrowing to a BO campaign, or a
    robustness matrix of method parameters. This is a complete, up-front design a human runs as a
    batch; it does not adapt to results the way `suggest_next_experiment` does.

    **A continuous factor is allowed and is held at the two ends of its declared range.** So
    temperature 20-120 enters the screen as 20 and 120 and nothing between — which is what a
    two-level screen *is*, and is fine for asking "does this factor matter at all". The return names
    every factor treated this way; say so to the chemist, because a temperature column reading
    20/120 looks like a considered choice of levels rather than a collapsed range. When the question
    is where in the range the optimum sits, that is `suggest_next_experiment`, not a screen.

    **When the full grid does not fit the plate, reduce it.** `n_generators` halves the run count
    per generator: seven two-level factors are 128 runs at 0, then 64, 32, 16 — so a screen that
    could not be run at all becomes one that fits 96 wells. Reduce only when the chemist's budget
    demands it, and never quietly: the returned design carries a `resolution` and a `summary`
    saying exactly which combinations were given up and which effects are confounded as a result.
    **Repeat that summary to the chemist.** A fractional design presented as if it were the whole
    screen is the failure this field exists to prevent. Every factor must have exactly two levels
    for a reduced design; a three-level categorical is refused rather than crossed in full.

    **Three knobs worth reaching for, and one caution on each.**

    - `n_center` adds centre runs at the midpoint of every continuous factor. They are the only
      thing in a two-level screen that can reveal curvature — a factor that helps up to a point and
      then hurts reads as "no effect" without them. Note the count: BoFire adds them **per
      combination of the categorical factors**, so the total is not `corners + n_center`.
    - `n_repetitions` replicates the design, which is what gives it a pure-error estimate; without
      any replication no effect the screen shows has a significance to quote.
    - `randomize` shuffles the run order, so a drift over the session (a decaying reagent, a warming
      room) is not read as a factor effect. Tell the chemist to run them in the order returned.

    Both `n_center` and `n_repetitions` need at least one continuous factor and are **refused** on
    an all-categorical problem: BoFire ignores them there, and a silently ignored argument is worse
    than an error. Repeat an all-categorical screen yourself if you want replicates.

    `n_center` is refused on one further shape — a *reduced* design (`n_generators > 0`) that also
    carries a categorical factor. The reduction re-encodes each category onto two numeric levels, so
    a centre row would place it at 0.5, which is neither of them. Ask for centre points on the full
    grid instead. `n_repetitions` is unaffected: a replicate repeats whole rows, so it needs no
    midpoint.

    **A problem carrying constraints is refused by the factorial.** It enumerates the corners of
    the space and honours no limit, so it would hand back runs that violate one. That is what
    `criterion` is for: pass `d-optimal` (or `i-optimal`, `a-optimal`, `space-filling`) with a run
    budget and the design honours the constraints instead, filling the budget exactly. Use it
    whenever the chemist has a real limit or a fixed number of runs rather than a grid.

    An optimality criterion designs for a **stated model**, so `formula` is part of the question: a
    `linear` design is blind to curvature by construction. Never report one without naming its
    model. `space-filling` assumes no model at all. Two things in that return to repeat rather than
    tidy away — a repeated row is intentional replication, and the design carries no alias
    structure, so unlike a fractional factorial it cannot say what is confounded.

    Args:
        problem: The decision variables and the objective (its direction is not used by a screening
            design, but the same `OptimizationProblem` shape is reused so observations from the
            screen can seed a follow-up `suggest_next_experiment` campaign).
        n_generators: 0 (default) for every combination. Each step above 0 halves the design and
            requires every factor to have exactly two levels.
        n_center: Centre runs per categorical combination. Needs a continuous factor, and is not
            available on a reduced design that also has categorical factors.
        n_repetitions: How many times to replicate the design. Needs a continuous factor.
        randomize: Shuffle the run order (reproducibly).
        criterion: `factorial` (the default, everything above) or one of `d-optimal`,
            `i-optimal`, `a-optimal`, `space-filling` — see the paragraph below.
        n_experiments: The run budget, required by every criterion except `factorial` and refused
            with it (a factorial's size is the grid, not a number you choose).
        formula: The model an optimality criterion is optimal *for*: `linear`,
            `linear-and-interactions`, `linear-and-quadratic`, `fully-quadratic`.

    Returns:
        For `factorial`, the runs plus `resolution`, `two_level_continuous` and a `summary` stating
        whether the design is exhaustive, what was collapsed and what is confounded. For every
        other criterion, the runs plus `formula`, `n_terms`, `duplicate_runs`,
        `honoured_constraints` and a `summary` of what that design cannot do.
    """
    problem = OptimizationProblem.model_validate(problem)
    require_names_do_not_clash(problem)
    if criterion != "factorial":
        return await asyncio.to_thread(optimal_design, problem, n_experiments, criterion, formula)
    if n_experiments:
        # A silently ignored argument is worse than an error: a factorial's size is fixed by its
        # level
        # counts, so a budget cannot be honoured.
        raise ValueError(
            f"a factorial's run count is the size of the grid, so n_experiments={n_experiments} "
            "cannot be honoured here. Pass a criterion that takes a budget — 'd-optimal' for "
            "estimating a model, 'space-filling' for covering the region — or reduce the grid with "
            "n_generators."
        )
    return await asyncio.to_thread(
        factorial_design,
        problem,
        n_generators,
        n_center,
        n_repetitions,
        randomize,
    )


class SurrogateAnswer(BaseModel):
    """What the model expects at points the chemist named, and how well it predicts (W5).

    Both halves come from **one** fit — the same fit `suggest_next_experiment` proposes from — so
    the prediction and the score it should be read against cannot describe two different models.
    """

    predictions: list[Prediction]
    fit: list[FitQuality]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def summary(self) -> str:
        """The fit quality first, because it is what licenses reading the predictions at all.

        An absent fit says so in words; a blank summary would read as "no caveat".
        """
        if not self.fit:
            return (
                "Fit quality was not assessed on this call, so nothing here says how well this "
                "model predicts runs it has not seen. Read the predictions as the model's belief, "
                "not as accuracy."
            )
        return " ".join([quality.summary for quality in self.fit])


@server.tool()
async def predict_outcome(
    problem: OptimizationProblem,
    observations: list[Observation] | str,
    points: list[dict[str, float | str]] | str,
    assess_fit: bool = True,
) -> SurrogateAnswer:
    """What does the model expect at *these* conditions? Ask instead of trusting a recommendation.

    Use this when the chemist names a point rather than asking what to run — "what would it give at
    90 °C in toluene with L3?", "is 60 °C enough?", "what about the corner we never tried?" — and
    when they want to know whether the model is worth listening to before they act on a suggestion.
    Answering from the surrogate is the difference between a number and a guess, and the surrogate
    it asks is the same one `suggest_next_experiment` proposes from.

    **This endorses nothing.** A candidate is the optimizer's recommendation; a prediction is an
    answer about a point you chose. Each `Prediction.summary` says so — quote it rather than
    presenting a prediction as a suggestion.

    **An unexplored corner is a posterior-sd question, and this is the tool for it.** To answer "has
    the search been circling one region, or is there somewhere it has not looked", predict at a few
    corners of the space and compare their `sds` to a point among the runs already done. A much
    larger sd is a region the model knows nothing about; a similar one means the search has already
    covered it. Do not answer that from the run list alone.

    **A point outside a continuous range is answered, not refused** — with `in_domain: false` and a
    much wider sd, because the model extrapolates rather than clamping. Say that the point is
    outside the declared range and that the mean there is unconstrained; do not quietly present it
    as a prediction like any other.

    **A categorical option the problem does not list is refused**, and the difference is real rather
    than a policy: a range has an outside the model can extrapolate into, and a category does not —
    a ligand that was never declared has no representation in the surrogate at all. Asking about one
    means adding it to the problem's `categories`, which makes it a different decision space with no
    history. The same applies to a value of the wrong kind (a word where a number belongs).

    Args:
        problem: The decision space and objective(s), in the same shape `suggest_next_experiment`
            takes — pass the one `resume_campaign` returned, or the one you just built.
        observations: The runs the model should learn from. At least two, and every run must give a
            value for every parameter the problem declares.
        points: The conditions to predict at, each naming **every** parameter — one dict per
            question. These are not proposals and are not recorded against the campaign.
        assess_fit: Cross-validate the surrogate and return the score (default true). It is what
            tells a chemist whether the prediction is worth anything, and it costs one refit per
            fold — measured at roughly three times the call's latency on a ten-run problem. Set it
            false only for a follow-up question in the same conversation, where the fit quality is
            already known and repaying it buys nothing.

    Returns:
        One `Prediction` per point — the predicted value, the posterior sd, and whether the point is
        inside the declared space — plus the cross-validated fit quality per objective.
    """
    problem = OptimizationProblem.model_validate(problem)
    asked: list[dict[str, ParamValue]] = [dict(point) for point in _as_list(points, "points")]
    history = [Observation.model_validate(item) for item in _as_list(observations, "observations")]
    require_names_do_not_clash(problem)
    _require_observed_params_match(problem, history)
    require_observations_cover_objectives(problem, history)
    _require_points_match(problem, asked)
    # Featurized as on the suggestion path, so the prediction uses the same surrogate model.
    featurized = await featurize_problem(properties_for(default_store()), problem)
    require_descriptors_distinguish_categories(featurized.problem)
    predictions, fit = await asyncio.to_thread(
        interrogate_surrogate, featurized.problem, history, asked, None, assess_fit
    )
    return SurrogateAnswer(predictions=predictions, fit=fit)
